"""
首都师范大学图书馆座位自动预约脚本
纯 HTTP 请求，无头运行，适合 Termux (Android) 配合 cron 使用
每天 06:28 cron 触发，06:29:00 开始高频抢占，持续 3 分钟（学校 6:30 开放）
"""

import base64
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ============================================================
# 配置区 - 通过环境变量(GitHub Secrets)或直接填写
# ============================================================
STUDENT_ID = "1251001025"
PASSWORD = "ctsg233738"
START_TIME = "10:30"
END_TIME = "22:50"
TEST_NAME = "自习"

# 北京时间 = UTC+8
TZ = timezone(timedelta(hours=8))

# 预约优先级：[(描述, roomId, 座位号范围), ...]
# roomId: 128255038=B区, 128255037=A区, 128255043=G区, 128255044=H区
# 座位范围 None=不限制；可按自己喜好调整区域和座位号
PRIORITY = [
    ("B区 B017-B040", 128255038, "B017", "B040"),
    ("A区 A001-A013", 128255037, "A001", "A013"),
    ("G区 全部",      128255043, None,   None),
    ("H区 全部",      128255044, None,   None),
    ("A区 兜底",      128255037, None,   None),
]

# ============================================================
BASE_URL = "https://selfservice.cnu.edu.cn/ic-web"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def encrypt_password(password: str, nonce: str, public_key_b64: str) -> str:
    """用 RSA 公钥加密 '密码;nonce'（模拟 JSEncrypt）"""
    der = base64.b64decode(public_key_b64)
    pubkey = serialization.load_der_public_key(der)
    combined = f"{password};{nonce}"
    encrypted = pubkey.encrypt(combined.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def api_get(session, url, **kw):
    """带调试的 GET 请求"""
    resp = session.get(url, timeout=15, **kw)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception:
        raise Exception(f"非JSON响应: {resp.text[:300]}")


def api_post_json(session, url, json_data, **kw):
    """带调试的 POST 请求"""
    resp = session.post(url, json=json_data, timeout=15, **kw)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception:
        raise Exception(f"非JSON响应: {resp.text[:300]}")


def login(session: requests.Session) -> tuple[str, int]:
    """登录获取 token 和 accNo"""
    log("获取公钥...")
    data = api_get(session, f"{BASE_URL}/login/publicKey", headers={"lan": "1"})
    if data["code"] != 0:
        raise Exception(f"获取公钥失败: {data['message']}")

    public_key = data["data"]["publicKey"]
    nonce_str = data["data"]["nonceStr"]
    log("加密密码...")
    encrypted_pwd = encrypt_password(PASSWORD, nonce_str, public_key)

    log("登录中...")
    data = api_post_json(session, f"{BASE_URL}/login/user", {
        "logonName": STUDENT_ID,
        "password": encrypted_pwd,
        "captcha": "",
        "consoleType": 16,
    }, headers={"lan": "1"})
    if data["code"] != 0:
        raise Exception(f"登录失败: {data['message']}")

    token = data["data"]["token"]
    acc_no = data["data"]["accNo"]
    log(f"登录成功, token={token[:16]}...")
    return token, acc_no


def get_available_seats(session: requests.Session, room_id: int, date_str: str) -> list:
    """获取指定区域当天的座位状态"""
    resp = session.get(
        f"{BASE_URL}/reserve",
        params={
            "roomIds": room_id,
            "resvDates": date_str,
            "sysKind": 8,
        },
    )
    data = resp.json()
    if data["code"] != 0:
        raise Exception(f"查询座位失败: {data['message']}")
    return data["data"]


def find_free_seat(seats: list, name_min: str = None, name_max: str = None) -> dict | None:
    """在座位列表中找全天空闲的座位，可选座位号范围过滤"""
    for seat in seats:
        reservations = seat.get("resvInfo") or []
        if len(reservations) > 0:
            continue
        name = seat["devName"]
        parts = name.split("-")
        seat_code = parts[-1] if len(parts) >= 2 else name
        if name_min is not None and seat_code < name_min:
            continue
        if name_max is not None and seat_code > name_max:
            continue
        return seat
    return None


def book_seat(session: requests.Session, dev_id: int, acc_no: int, date_str: str,
              start_time: str, end_time: str) -> dict:
    """提交预约"""
    payload = {
        "sysKind": 8,
        "appAccNo": acc_no,
        "memberKind": 1,
        "resvMember": [acc_no],
        "resvBeginTime": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {start_time}:00",
        "resvEndTime": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {end_time}:00",
        "testName": TEST_NAME,
        "captcha": "",
        "resvProperty": 0,
        "resvDev": [dev_id],
        "memo": "",
    }
    resp = session.post(f"{BASE_URL}/reserve", json=payload)
    return resp.json()


def main():
    log("===== CNU 座位自动预约 =====")

    if PASSWORD == "你的图书馆密码":
        log("错误: 请先在脚本中设置 PASSWORD")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Referer": "https://selfservice.cnu.edu.cn/",
    })

    # 1. 登录
    try:
        token, acc_no = login(session)
    except Exception as e:
        log(f"登录失败: {e}")
        sys.exit(1)

    session.headers.update({"token": token})

    # 2. 日期（北京时间）
    now = datetime.now(TZ)
    today = now.strftime("%Y%m%d")
    log(f"目标: {today} {START_TIME}-{END_TIME}")

    # 3. 等待到 06:29:00 再开始
    target = now.replace(hour=6, minute=29, second=0, microsecond=0)
    wait_sec = (target - datetime.now(TZ)).total_seconds()
    if wait_sec > 0:
        log(f"等待至 {target.strftime('%H:%M:%S')} 开始抢占...")
        time.sleep(wait_sec)
    else:
        log("已过 06:29，立即开始抢占")

    # 4. 高频抢占：持续 3 分钟，每秒 3 轮
    deadline = datetime.now(TZ) + timedelta(minutes=3)
    attempt = 0
    booked = False
    log(f"开始抢占，截止 {deadline.strftime('%H:%M:%S')}")

    while datetime.now(TZ) < deadline:
        attempt += 1
        for desc, room_id, name_min, name_max in PRIORITY:
            try:
                seats = get_available_seats(session, room_id, today)
            except Exception:
                continue

            seat = find_free_seat(seats, name_min, name_max)
            if not seat:
                continue

            dev_id = seat["devId"]
            try:
                result = book_seat(session, dev_id, acc_no, today, START_TIME, END_TIME)
            except Exception:
                continue

            if result["code"] == 0:
                log(f"[OK] 第{attempt}轮 {desc}: {seat['devName']} 预约成功!")
                log(f"  编号: {result['data']['resvId']}")
                booked = True
                break

        if booked:
            break
        time.sleep(0.33)

    if not booked:
        log(f"[FAIL] 3 分钟内共 {attempt} 轮均未成功")
        sys.exit(1)

    log("===== 完成 =====")


if __name__ == "__main__":
    main()
