"""
首都师范大学图书馆座位自动预约脚本
纯 HTTP 请求，无头运行，适合 Termux (Android) 配合 cron 使用
每天 06:15 cron 触发，06:29:58 开始抢占，持续 3 分钟（学校 6:30 开放预约）
"""

import base64
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
START_TIME = "12:30"
END_TIME = "22:50"
TEST_NAME = "自习"

# 北京时间 = UTC+8
TZ = timezone(timedelta(hours=8))

# 预约优先级：[(描述, roomId, 座位号范围), ...]
# roomId: 128255037=A区, 128255038=B区, 128255039=C区, 128255040=D区,
#         128255041=E区, 128255042=F区, 128255043=G区, 128255044=H区
#         以上 037-044 均为良乡二层；128255036=良乡一层A区（81座，仅作终极兜底）
# 座位名格式: 良-2A001，split 后得 "2A001"，范围需含楼层前缀 "2"
PRIORITY = [
    ("二层B区 B018-B040", 128255038, "2B018", "2B040"),
    ("一层A区 A001-A013", 128255036, "1A001", "1A013"),
    ("二层G区 全部",      128255043, None,   None),
    ("二层H区 全部",      128255044, None,   None),
    ("一层A区 兜底",      128255036, None,   None),
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
    data = api_get(session, f"{BASE_URL}/reserve", params={
        "roomIds": room_id,
        "resvDates": date_str,
        "sysKind": 8,
    })
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
    return api_post_json(session, f"{BASE_URL}/reserve", {
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
    })


def wait_until(target: datetime):
    """睡眠到目标时间，每60秒打印一次心跳"""
    while True:
        remaining = (target - datetime.now(TZ)).total_seconds()
        if remaining <= 0:
            return
        if remaining > 60:
            log(f"  等待中... 距目标还有 {int(remaining//60)} 分 {int(remaining%60)} 秒")
            time.sleep(60)
        else:
            time.sleep(remaining)
            return


def do_login(session: requests.Session) -> tuple[str, int]:
    """登录并更新 session header，返回 (token, accNo)"""
    token, acc_no = login(session)
    session.headers.update({"token": token})
    return token, acc_no


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

    now = datetime.now(TZ)

    # 确定抢座目标日期和各时间节点
    # 如果当前已过 06:30，目标设为明天；否则设为今天
    if now.hour > 6 or (now.hour == 6 and now.minute >= 30):
        target_date = now + timedelta(days=1)
    else:
        target_date = now

    target_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    t_first_login = target_day.replace(hour=6, minute=0)   # 06:00 第一次登录
    t_refresh     = target_day.replace(hour=6, minute=25)  # 06:25 刷新token
    t_start       = target_day.replace(hour=6, minute=29, second=58)  # 06:29:58 开抢

    today = target_date.strftime("%Y%m%d")
    log(f"目标日期: {today}  预约时段: {START_TIME}-{END_TIME}")
    log(f"计划: 立即登录 → 06:00 刷新token → 06:25 再次刷新 → 06:29:58 开抢")

    # 0. 启动时立即登录一次，验证账号可用
    try:
        log("--- 启动登录 ---")
        token, acc_no = do_login(session)
    except Exception as e:
        log(f"启动登录失败: {e}")
        sys.exit(1)

    # 1. 等待到 06:00，刷新 token
    if datetime.now(TZ) < t_first_login:
        log(f"等待至 {t_first_login.strftime('%H:%M:%S')} 刷新token...")
        wait_until(t_first_login)

        try:
            log("--- 06:00 刷新token ---")
            token, acc_no = do_login(session)
        except Exception as e:
            log(f"06:00 刷新token失败: {e}，使用旧token继续")

    # 2. 等待到 06:25，再次刷新 token
    if datetime.now(TZ) < t_refresh:
        log(f"等待至 {t_refresh.strftime('%H:%M:%S')} 再次刷新token...")
        wait_until(t_refresh)

        try:
            log("--- 06:25 刷新token ---")
            token, acc_no = do_login(session)
        except Exception as e:
            log(f"06:25 刷新token失败: {e}，使用旧token继续")

    # 3. 等待到 06:29:58 开始抢占
    if datetime.now(TZ) < t_start:
        log(f"等待至 {t_start.strftime('%H:%M:%S')} 开始抢占...")
        wait_until(t_start)
    else:
        log("已过 06:29:58，立即开始抢占")

    # 4. 高频抢占：持续 3 分钟
    deadline = datetime.now(TZ) + timedelta(minutes=3)
    attempt = 0
    booked = False
    log(f"开始抢占，截止 {deadline.strftime('%H:%M:%S')}")

    while datetime.now(TZ) < deadline:
        attempt += 1
        for desc, room_id, name_min, name_max in PRIORITY:
            try:
                seats = get_available_seats(session, room_id, today)
            except Exception as e:
                if attempt <= 5:
                    log(f"  [{desc}] 查询异常: {e}")
                continue

            if attempt <= 3:
                free_names = []
                for s in seats:
                    r = s.get("resvInfo") or []
                    if len(r) == 0:
                        free_names.append(s["devName"])
                log(f"  [{desc}] 共{len(seats)}个座位, 空闲{len(free_names)}个: {free_names[:5]}...")

            seat = find_free_seat(seats, name_min, name_max)
            if not seat:
                continue

            dev_id = seat["devId"]
            try:
                result = book_seat(session, dev_id, acc_no, today, START_TIME, END_TIME)
            except Exception as e:
                if attempt <= 5:
                    log(f"  [{desc}] 预约异常: {e}")
                continue

            if result["code"] == 0:
                log(f"[OK] 第{attempt}轮 {desc}: {seat['devName']} 预约成功!")
                log(f"  编号: {result['data']['resvId']}")
                booked = True
                break
            else:
                if attempt <= 5:
                    log(f"  [{desc}] 预约被拒: {result['message']}")

        if booked:
            break
        time.sleep(0.15)

    if not booked:
        log(f"[FAIL] 3 分钟内共 {attempt} 轮均未成功")
        sys.exit(1)

    log("===== 完成 =====")


if __name__ == "__main__":
    main()
