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

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def sha1_encrypt(salt: str, password: str) -> str:
    return hashlib.sha1(f"{salt}-{password}".encode('utf-8')).hexdigest()

def get_login_salt(session: requests.Session) -> str:
    resp = session.get(f"{BASE_URL}/student/login-salt", timeout=5)
    return resp.text.strip()

def login(session: requests.Session, username, password) -> dict:
    session.get(f"{BASE_URL}/student/login", timeout=5)
    salt = get_login_salt(session)
    encrypted_password = sha1_encrypt(salt, password)
    data = {"username": username, "password": encrypted_password, "captcha": "", "terminal": "student"}
    resp = session.post(f"{BASE_URL}/student/login", json=data, headers={"Content-Type": "application/json"}, timeout=5)
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
        password = str(req_data.get('password', ''))
        req_week = req_data.get('week')
        mode = req_data.get('mode', 'snapshot')

        if not username or not password:
            return jsonify({"code": 400, "msg": "请求失败：学号和密码不能为空！"}), 400

        current_auth_hash = hashlib.sha256(f"{username}_{password}".encode('utf-8')).hexdigest()
        cache_file_path = os.path.join(CACHE_DIR, f"{username}.json")

        has_cache = os.path.exists(cache_file_path)
        local_cache = None
        if has_cache:
            try:
                with open(cache_file_path, "r", encoding="utf-8") as f:
                    local_cache = json.load(f)
            except:
                has_cache = False

        # ------------------ 路线一：快照闪电通道 ------------------
        if mode == 'snapshot':
            if not has_cache or not local_cache:
                return jsonify({"code": 204, "msg": "本地无快照，需要完整建立教务同步"}), 200

            if current_auth_hash != local_cache.get("shadow_auth"):
                return jsonify({"code": 401, "msg": "凭证与本地离线记录不匹配"}), 401

            last_fetch_time = local_cache.get("last_fetch_time", 0)
            is_fresh = (time.time() - last_fetch_time < 600)

            resp_payload = local_cache.get("schedule_data", {})
            resp_payload["status"] = "fresh" if is_fresh else "stale"
            return jsonify(resp_payload), 200

        # ------------------ 路线二：强连网络通道 ------------------
        if mode == 'network':
            try:
                session = requests.Session()
                # 注入行业标准浏览器 User-Agent，防止部分教务组件对空网头返回截断数据
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                })

                login_result = login(session, username, password)
                if not login_result.get("result", False):
                    return jsonify({"code": 401, "msg": "教务系统登录失败，请检查学号和密码是否正确"}), 401

                # 1. 优先拉取课表主页面和基础数据包
                page_resp = session.get(f"{BASE_URL}/student/for-std/course-table?bizTypeId=2", timeout=5)
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

                # 2. 顺势调取基础学期及课程ID数据包
                resp = session.get(f"{BASE_URL}/student/for-std/course-table/get-data", params={"bizTypeId": 2, "semesterId": semester["id"]}, timeout=5)
                course_data = resp.json()

                # 3. 三路联合防御打捞 stdPersonId
                std_person_id = None
                id_patterns = [
                    r'stdPersonId["\']?\s*[:=]\s*["\']?(\d+)',
                    r'studentId["\']?\s*[:=]\s*["\']?(\d+)',
                    r'personId["\']?\s*[:=]\s*["\']?(\d+)',
                    r'stdPersonId=(\d+)',
                    r'studentId=(\d+)'
                ]

                # 策略 A：从已拉回的主 HTML 源码中深度指纹匹配
                for pattern in id_patterns:
                    match = re.search(pattern, html_content, re.IGNORECASE)
                    if match:
                        std_person_id = int(match.group(1))
                        break

                # 策略 B：检查 course_data JSON 返回字典中是否自带学籍主体标识
                if not std_person_id and isinstance(course_data, dict):
                    for key in ["studentId", "stdPersonId", "personId", "id"]:
                        if key in course_data and course_data[key]:
                            std_person_id = int(course_data[key])
                            break

                # 策略 C：传统兜底，对侧边菜单接口发起捞取
                if not std_person_id:
                    try:
                        menu_resp = session.post(f"{BASE_URL}/student/ws/menu/get-menus", json={}, timeout=3)
                        for pattern in id_patterns:
                            match = re.search(pattern, menu_resp.text, re.IGNORECASE)
                            if match:
                                std_person_id = int(match.group(1))
                                break
                    except:
                        pass

                if not std_person_id:
                    return jsonify({"code": 500, "msg": "教务系统策略更新：未能自动解析到该学生的人员内部ID(stdPersonId)。"}), 500

                # 4. 执行最终课表矩阵数据获取
                if not req_week:
                    req_week = course_data["currentWeek"]

                datum_payload = {
                    "lessonIds": course_data["lessonIds"],
                    "studentId": None,
                    "stdPersonId": int(std_person_id),
                    "weekIndex": int(req_week)
                }

                schedule_resp = session.post(f"{BASE_URL}/student/ws/schedule-table/datum", json=datum_payload, headers={"Content-Type": "application/json"}, timeout=5)
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
                    "schedule": matrix,
                    "status": "fresh"
                }

                # 同步回本地沙盒快照
                serialized_cache = {
                    "shadow_auth": current_auth_hash,
                    "last_fetch_time": time.time(),
                    "schedule_data": success_payload
                }
                with open(cache_file_path, "w", encoding="utf-8") as f:
                    json.dump(serialized_cache, f, ensure_ascii=False, indent=4)

                return jsonify(success_payload), 200

            except requests.exceptions.RequestException:
                if has_cache and local_cache:
                    if current_auth_hash == local_cache.get("shadow_auth"):
                        fallback_payload = local_cache.get("schedule_data", {})
                        fallback_payload["status"] = "jwxt_collapsed"
                        return jsonify(fallback_payload), 200
                    else:
                        return jsonify({"code": 401, "msg": "教务网突发故障，且输入的