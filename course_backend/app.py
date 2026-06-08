#!/usr/bin/env python3
import os
import time
import json
import re
import hashlib
import traceback
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

BASE_URL = "https://jwxt.aqnu.edu.cn"
WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

# 自动在宝塔本地项目根目录下建立沙盒缓存文件夹
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def sha1_encrypt(salt: str, password: str) -> str:
    return hashlib.sha1(f"{salt}-{password}".encode('utf-8')).hexdigest()

def get_login_salt(session: requests.Session) -> str:
    resp = session.get(f"{BASE_URL}/student/login-salt")
    return resp.text.strip()

def login(session: requests.Session, username, password) -> dict:
    session.get(f"{BASE_URL}/student/login")
    salt = get_login_salt(session)
    encrypted_password = sha1_encrypt(salt, password)
    data = {"username": username, "password": encrypted_password, "captcha": "", "terminal": "student"}
    resp = session.post(f"{BASE_URL}/student/login", json=data, headers={"Content-Type": "application/json"})
    try:
        return resp.json()
    except:
        return {"result": False, "raw_text": resp.text[:200]}

def hhmm_to_minutes(time_val: int) -> int:
    if time_val is None: return 0
    return (time_val // 100) * 60 + (time_val % 100)

def get_start_section(start_time: int) -> int:
    start_map = {800: 1, 855: 2, 1000: 3, 1055: 4, 1400: 5, 1450: 6, 1540: 7, 1645: 8, 1735: 9, 1855: 10, 1950: 11, 2045: 12}
    target_min = hhmm_to_minutes(start_time)
    closest_time = min(start_map.keys(), key=lambda k: abs(hhmm_to_minutes(k) - target_min))
    return start_map[closest_time]

def get_end_section(end_time: int) -> int:
    end_map = {845: 1, 940: 2, 1045: 3, 1140: 4, 1445: 5, 1535: 6, 1625: 7, 1730: 8, 1820: 9, 1940: 10, 2035: 11, 2130: 12}
    target_min = hhmm_to_minutes(end_time)
    closest_time = min(end_map.keys(), key=lambda k: abs(hhmm_to_minutes(k) - target_min))
    return end_map[closest_time]

@app.route('/api/schedule', methods=['POST'])
def get_schedule():
    try:
        req_data = request.get_json() or {}
        username = str(req_data.get('username', '')).strip()
        password = req_data.get('password', '')
        req_week = req_data.get('week')

        if not username or not password:
            return jsonify({"code": 400, "msg": "请求失败：学号和密码不能为空！"}), 400

        # 【核心安全防御】在本地计算本次请求的双因子影子凭证哈希
        current_auth_hash = hashlib.sha256(f"{username}_{password}".encode('utf-8')).hexdigest()
        cache_file_path = os.path.join(CACHE_DIR, f"{username}.json")

        # 尝试读取属于该用户的本地专属沙盒数据
        has_cache = os.path.exists(cache_file_path)
        local_cache = None
        if has_cache:
            try:
                with open(cache_file_path, "r", encoding="utf-8") as f:
                    local_cache = json.load(f)
            except:
                has_cache = False

        # ---- 阶梯一：10分钟内高频请求保护风控 ----
        if has_cache and local_cache:
            last_fetch_time = local_cache.get("last_fetch_time", 0)
            # 如果距离上一次成功抓取未满 10 分钟 (600秒)
            if time.time() - last_fetch_time < 600:
                # 必须过一道影子凭证卡口，严防他人拿着对的学号和错的密码来撞库
                if current_auth_hash == local_cache.get("shadow_auth"):
                    resp_payload = local_cache.get("schedule_data", {})
                    resp_payload["is_cache"] = True  # 打上缓存标记
                    return jsonify(resp_payload)
                else:
                    return jsonify({"code": 401, "msg": "请求过于频繁，且密码与本地记录不匹配，拒绝访问。"}), 401

        # ---- 阶梯二：越过缓存期，冲向学校教务网 ----
        try:
            session = requests.Session()
            login_result = login(session, username, password)

            # 情况 A：教务系统在线，但亲自判定了密码错误或账号不存在
            if not login_result.get("result", False):
                return jsonify({"code": 401, "msg": "教务系统登录失败，请检查学号和密码是否正确", "debug_login": login_result}), 401

            # 情况 B：教务网正常，登录成功，开始多级特征智能抓取
            std_person_id = None

            # 梯队 1：从通知接口抓取
            try:
                notif_resp = session.post(f"{BASE_URL}/student/ws/notification/get-alert-notifications", json={}, timeout=4)
                if notif_resp.status_code != 200:
                    notif_resp = session.get(f"{BASE_URL}/student/ws/notification/get-alert-notifications", timeout=4)
                id_match = re.search(r'"personAssoc"\s*:\s*(\d{5,8})', notif_resp.text)
                if id_match:
                    std_person_id = int(id_match.group(1))
            except:
                pass

            # 梯队 2：从系统菜单接口抠取
            if not std_person_id:
                try:
                    menu_resp = session.post(f"{BASE_URL}/student/ws/menu/get-menus", json={}, timeout=4)
                    if menu_resp.status_code != 200:
                        menu_resp = session.get(f"{BASE_URL}/student/ws/menu/get-menus", timeout=4)
                    id_match = re.search(r'stdPersonId=(\d{5,8})', menu_resp.text)
                    if id_match:
                        std_person_id = int(id_match.group(1))
                except:
                    pass

            if not std_person_id:
                return jsonify({"code": 500, "msg": "教务网在线，但多级特征扫描完毕未能自动解析到该学生人员内部ID(stdPersonId)。"}), 500

            # 抓取学期元数据
            page_resp = session.get(f"{BASE_URL}/student/for-std/course-table?bizTypeId=2")
            html_content = page_resp.text

            sem_match = re.search(r"var semesters = JSON\.parse\(\s*'([^']+)'", html_content)
            if not sem_match:
                return jsonify({"code": 500, "msg": "无法从教务系统页面中提取学期配置元数据"}), 500

            semesters = json.loads(sem_match.group(1).replace('\\"', '"'))
            now_dt = datetime.now()
            semester = None
            for sem in semesters:
                start = datetime.strptime(sem["startDate"], "%Y-%m-%d")
                end = datetime.strptime(sem["endDate"], "%Y-%m-%d")
                if start <= now_dt <= end:
                    semester = sem
                    break

            if not semester:
                return jsonify({"code": 500, "msg": "当前时间未落在教务系统的任何学期范围内"}), 500

            resp = session.get(f"{BASE_URL}/student/for-std/course-table/get-data", params={"bizTypeId": 2, "semesterId": semester["id"]})
            course_data = resp.json()

            if not req_week:
                req_week = course_data["currentWeek"]

            datum_payload = {
                "lessonIds": course_data["lessonIds"],
                "studentId": None,
                "stdPersonId": int(std_person_id),
                "weekIndex": int(req_week)
            }

            schedule_resp = session.post(f"{BASE_URL}/student/ws/schedule-table/datum", json=datum_payload, headers={"Content-Type": "application/json"})
            schedule_data = schedule_resp.json()

            lessons = schedule_data["result"]["lessonList"]
            schedules = schedule_data["result"]["scheduleList"]

            lesson_map = {lesson["id"]: lesson for lesson in lessons}
            matrix = {day: [] for day in WEEKDAY_NAMES.values()}

            for s in schedules:
                day = WEEKDAY_NAMES.get(s["weekday"], "")
                if not day: continue
                lesson = lesson_map.get(s["lessonId"], {})

                room_info = "未知教室"
                if s.get("room") and isinstance(s.get("room"), dict):
                    room_info = s["room"].get("nameZh") or "未知教室"
                elif s.get("roomName"):
                    room_info = s.get("roomName")

                start_section = get_start_section(s["startTime"])
                end_section = get_end_section(s["endTime"])
                if end_section < start_section: end_section = start_section

                matrix[day].append({
                    "start": start_section,
                    "end": end_section,
                    "course": lesson.get("courseName", ""),
                    "teacher": ", ".join([t["name"] for t in lesson.get("teacherAssignmentList", [])]),
                    "room": room_info
                })

            success_payload = {
                "code": 200,
                "semesterName": semester['nameZh'],
                "currentWeek": course_data["currentWeek"],
                "selectedWeek": req_week,
                "schedule": matrix
            }

            # 【冷冻锁同步】将最新的课表、影子凭证哈希和当前时间戳牢牢锁入用户的私有沙盒中
            serialized_cache = {
                "shadow_auth": current_auth_hash,
                "last_fetch_time": time.time(),
                "schedule_data": success_payload
            }
            with open(cache_file_path, "w", encoding="utf-8") as f:
                json.dump(serialized_cache, f, ensure_ascii=False, indent=4)

            return jsonify(success_payload)

        except requests.exceptions.RequestException:
            # ---- 阶梯三：进入捕获：教务网彻底瘫痪/超时/502 ----
            if has_cache and local_cache:
                # 机主本人输入正确密码：哈希完美契合，安全降级吐出专属于他的残像
                if current_auth_hash == local_cache.get("shadow_auth"):
                    fallback_payload = local_cache.get("schedule_data", {})
                    fallback_payload["is_fallback"] = True  # 打上战时残像降级标记
                    return jsonify(fallback_payload)
                else:
                    # 恶作剧/探路人（路人乙）输入了错密：影子凭证无情阻断，严防残像冒领与数据污染！
                    return jsonify({"code": 401, "msg": "学校教务网目前崩溃，且您的凭证与本地离线记录不匹配，拒绝提供残像服务。"}), 401
            else:
                return jsonify({"code": 502, "msg": "网络故障：学校教务系统崩溃，且本地无该学号的课表残像记录，请稍后再试。"}), 502

    except Exception as server_err:
        return jsonify({
            "code": 500,
            "msg": f"后端运行时遭遇致命崩溃: {str(server_err)}",
            "traceback": traceback.format_exc()
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
