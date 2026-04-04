import random
from flask import Flask, request, jsonify, send_file
import subprocess, os, threading, uuid, time, logging, requests
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
RECORDINGS_DIR = "/tmp/recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
active_jobs = {}
JOBS_FILE = "/tmp/jobs.json"

def save_jobs():
    try:
        import json
        # Only save non-process data
        saveable = {}
        for jid, job in active_jobs.items():
            saveable[jid] = {k:v for k,v in job.items() if k != "process"}
        with open(JOBS_FILE, "w") as f:
            json.dump(saveable, f)
    except Exception as e:
        logger.warning(f"save_jobs error: {e}")

def load_jobs():
    try:
        import json
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE) as f:
                return json.load(f)
    except: pass
    return {}

# Load existing jobs on startup
active_jobs.update(load_jobs())
monitored_accounts = {}
monitoring_active = False
NTFY_TOPIC = "Inslive-jhwalker"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

def send_notification(title, message, priority="high", tags="movie_camera"):
    try:
        requests.post(NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10)
        logger.info(f"Ntfy sent: {title}")
    except Exception as e:
        logger.error(f"Ntfy failed: {e}")

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "InstaLive Recorder Server",
                    "active_jobs": len(active_jobs), "monitoring": monitoring_active})

@app.route("/record", methods=["POST"])
def start_recording():
    data = request.json
    if not data: return jsonify({"error": "No data"}), 400
    username = data.get("username")
    cookie = data.get("cookie")
    from_start = data.get("from_start", True)
    if not username or not cookie:
        return jsonify({"error": "username and cookie required"}), 400
    # Check if already recording
    for jid, job in active_jobs.items():
        if job["username"]==username and job["status"] in ["recording","starting"]:
            return jsonify({"job_id": jid, "status": "already_recording"})
    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=record_live, args=(job_id, username, cookie, from_start))
    thread.daemon = True
    thread.start()
    active_jobs[job_id] = {
        "status": "starting", "username": username,
        "started_at": time.time(), "file": None,
        "error": None, "process": None
    }
    logger.info(f"Started job {job_id} for @{username}")
    return jsonify({"job_id": job_id, "status": "started"})

@app.route("/monitor/start", methods=["POST"])
def start_monitoring():
    global monitoring_active
    data = request.json
    if not data: return jsonify({"error": "No data"}), 400
    account_id = data.get("account_id")
    cookie = data.get("cookie")
    if not account_id or not cookie:
        return jsonify({"error": "account_id and cookie required"}), 400
    monitored_accounts[account_id] = {"cookie": cookie}
    if not monitoring_active:
        monitoring_active = True
        t = threading.Thread(target=monitor_loop)
        t.daemon = True
        t.start()
        logger.info("Monitoring started")
        send_notification("InstaLive Server", "24/7 monitoring active!", priority="low", tags="eyes")
    return jsonify({"status": "monitoring", "accounts": len(monitored_accounts)})

@app.route("/monitor/stop", methods=["POST"])
def stop_monitoring():
    global monitoring_active
    data = request.json
    account_id = data.get("account_id") if data else None
    if account_id and account_id in monitored_accounts:
        del monitored_accounts[account_id]
    if not monitored_accounts:
        monitoring_active = False
    return jsonify({"status": "stopped"})

@app.route("/monitor/status", methods=["GET"])
def monitor_status():
    return jsonify({
        "active": monitoring_active,
        "accounts": len(monitored_accounts),
        "active_recordings": [j["username"] for j in active_jobs.values() if j["status"]=="recording"]
    })

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    if job_id not in active_jobs:
        return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    response = {"job_id": job_id, "status": job["status"],
                "username": job["username"], "error": job["error"]}
    if job["status"]=="done" and job["file"] and os.path.exists(job["file"]):
        response["file_size"] = os.path.getsize(job["file"])
        response["ready_to_download"] = True
    return jsonify(response)

@app.route("/jobs", methods=["GET"])
def list_jobs():
    return jsonify({"jobs": [
        {"job_id": jid, "username": j["username"], "status": j["status"]}
        for jid,j in active_jobs.items()
    ]})

@app.route("/download/<job_id>", methods=["GET"])
def download_file(job_id):
    if job_id not in active_jobs:
        return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job["status"]!="done":
        return jsonify({"error": "Not finished", "status": job["status"]}), 202
    if not job["file"] or not os.path.exists(job["file"]):
        return jsonify({"error": "File not found"}), 404
    return send_file(job["file"], as_attachment=True,
                     download_name=f"{job['username']}_live.mp4",
                     mimetype="video/mp4")

@app.route("/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    if job_id not in active_jobs:
        return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job.get("process"):
        try: job["process"].terminate()
        except: pass
    if job.get("file") and os.path.exists(job["file"]):
        try: os.remove(job["file"])
        except: pass
    del active_jobs[job_id]
    return jsonify({"status": "deleted"})

def monitor_loop():
    global monitoring_active
    logger.info("Monitor loop started")
    while monitoring_active and monitored_accounts:
        for account_id, account in list(monitored_accounts.items()):
            try:
                check_account_for_live(account_id, account["cookie"])
            except Exception as e:
                logger.error(f"Monitor error: {e}")
        time.sleep(60)
    monitoring_active = False

def check_account_for_live(account_id, cookie):
    try:
        headers = {
            "Cookie": cookie,
            "User-Agent": "Instagram 219.0.0.12.117 Android",
            "X-IG-App-ID": "936619743392459"
        }
        resp = requests.get("https://i.instagram.com/api/v1/feed/reels_tray/",
                           headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Reels tray {resp.status_code}")
            return
        tray = resp.json().get("tray", [])
        for item in tray:
            broadcast = item.get("broadcast")
            if not broadcast: continue
            user = item.get("user", {})
            username = user.get("username", "")
            if not username: continue
            already = any(j["username"]==username and j["status"] in ["recording","starting"]
                         for j in active_jobs.values())
            if already: continue
            viewers = broadcast.get("viewer_count", 0)
            logger.info(f"LIVE DETECTED: @{username} viewers={viewers}")
            send_notification(
                f"🔴 @{username} is LIVE!",
                f"Auto-recording started!\n👥 {viewers} viewers",
                priority="urgent", tags="red_circle,movie_camera"
            )
            job_id = str(uuid.uuid4())[:8]
            t = threading.Thread(target=record_live, args=(job_id, username, cookie, True))
            t.daemon = True
            t.start()
            active_jobs[job_id] = {
                "status": "starting", "username": username,
                "started_at": time.time(), "file": None,
                "error": None, "process": None
            }
    except Exception as e:
        logger.error(f"check_account error: {e}")

def record_live(job_id, username, cookie, from_start=True):
    output_file = os.path.join(RECORDINGS_DIR, f"{job_id}_{username}.mp4")
    active_jobs[job_id]["status"] = "recording"
    save_jobs()
    active_jobs[job_id]["file"] = output_file
    success = False
    # Try yt-dlp first
    try:
        logger.info(f"Job {job_id}: Trying yt-dlp for @{username}")
        cmd = [
            "yt-dlp",
            "--add-header", f"Cookie:{cookie}",
            "--add-header", "User-Agent:Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/105.0 Mobile Safari/537.36",
            "--add-header", "X-IG-App-ID:936619743392459",
            "-f", "best",
            "-o", output_file,
            "--no-part",
            "--retries", "10",
            "--fragment-retries", "10",
            "--extractor-retries", "5",
        ]
        if from_start:
            cmd.append("--live-from-start")
        cmd.append(f"https://www.instagram.com/{username}/live/")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        active_jobs[job_id]["process"] = process
        for line in process.stdout:
            if line.strip():
                logger.info(f"[{job_id}] {line.strip()}")
        process.wait()
        if (process.returncode==0 and os.path.exists(output_file)
                and os.path.getsize(output_file) > 10240):
            success = True
            logger.info(f"yt-dlp succeeded for @{username}")
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    # If yt-dlp failed try with sessionid cookie only
    if not success:
        try:
            logger.info(f"Job {job_id}: Retrying with sessionid only for @{username}")
            # Extract just sessionid from cookie
            sessionid = ""
            for part in cookie.split(";"):
                if "sessionid=" in part:
                    sessionid = part.strip()
                    break
            if sessionid:
                output_file2 = output_file.replace(".mp4", "_retry.mp4")
                cmd2 = [
                    "yt-dlp",
                    "--add-header", f"Cookie:{sessionid}",
                    "--add-header", "User-Agent:Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/105.0 Mobile Safari/537.36",
                    "-f", "best",
                    "-o", output_file2,
                    "--no-part",
                    "--retries", "5",
                    f"https://www.instagram.com/{username}/live/"
                ]
                process2 = subprocess.Popen(cmd2, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True)
                active_jobs[job_id]["process"] = process2
                for line in process2.stdout:
                    if line.strip(): logger.info(f"[{job_id}_retry] {line.strip()}")
                process2.wait()
                if (process2.returncode==0 and os.path.exists(output_file2)
                        and os.path.getsize(output_file2) > 10240):
                    output_file = output_file2
                    active_jobs[job_id]["file"] = output_file
                    success = True
                    logger.info(f"Retry succeeded for @{username}")
        except Exception as e:
            logger.error(f"Retry error: {e}")
    if success:
        active_jobs[job_id]["status"] = "done"
        save_jobs()
        size_mb = os.path.getsize(output_file) / (1024*1024)
        send_notification(
            f"✅ @{username} recorded!",
            f"Recording complete!\n📁 {size_mb:.1f} MB\nOpen app to download",
            priority="high", tags="white_check_mark"
        )
        logger.info(f"Job {job_id} done: {size_mb:.1f} MB")
    else:
        active_jobs[job_id]["status"] = "failed"
        save_jobs()
        active_jobs[job_id]["error"] = "All recording attempts failed. Cookie may be expired."
        send_notification(
            f"⚠️ Failed - @{username}",
            "Recording failed. Please refresh your Instagram login in the app.",
            priority="default", tags="warning"
        )
        logger.error(f"Job {job_id} failed for @{username}")
    threading.Timer(3600, lambda: cleanup_job(job_id)).start()

def cleanup_job(job_id):
    if job_id in active_jobs:
        job = active_jobs[job_id]
        if job.get("file") and os.path.exists(job["file"]):
            try: os.remove(job["file"])
            except: pass
        del active_jobs[job_id]



@app.route("/discover", methods=["POST"])
def discover_lives():
    data = request.json
    if not data: return jsonify({"error":"No data"}),400
    cookie = data.get("cookie","")
    if not cookie: return jsonify({"error":"cookie required"}),400

    lives = []
    seen = set()

    versions = ["269.0.0.18.75","264.0.0.19.102","261.0.0.21.111"]
    devices = [
        "28/9; 420dpi; 1080x1920; samsung; SM-G998B; b0q; qcom",
        "29/10; 440dpi; 1080x2340; OnePlus; IN2023; OnePlus8T; qcom",
        "30/11; 480dpi; 1080x2400; Xiaomi; M2007J3SG; apollo; qcom"
    ]
    idx = random.randint(0,len(versions)-1)
    headers = {
        "Cookie": cookie,
        "User-Agent": f"Instagram/{versions[idx]} Android ({devices[idx]}; en_US; 567067343352427)",
        "X-IG-App-ID": "567067343352427",
        "X-IG-Capabilities": "3brTvwE=",
        "X-IG-Connection-Type": "WIFI",
        "X-FB-HTTP-Engine": "Liger",
        "Accept-Language": "en-US",
        "Accept": "*/*"
    }

    def add_live(username, full_name, pic, viewers, thumb, source):
        if username and username not in seen:
            seen.add(username)
            lives.append({
                "username": username,
                "full_name": full_name,
                "profile_pic": pic,
                "viewers": viewers,
                "thumbnail": thumb,
                "source": source
            })

    # 1. Reels tray (followed accounts)
    try:
        r = requests.get(
            "https://i.instagram.com/api/v1/feed/reels_tray/",
            headers=headers, timeout=15)
        if r.status_code == 200:
            for item in r.json().get("tray",[]):
                b = item.get("broadcast")
                u = item.get("user",{})
                if b and u.get("username"):
                    add_live(u["username"],u.get("full_name",""),
                        u.get("profile_pic_url",""),
                        b.get("viewer_count",0),
                        b.get("cover_frame_url",""),"following")
    except Exception as e:
        logger.error(f"reels_tray: {e}")

    # 2. Top live (personalized explore)
    try:
        r = requests.get(
            "https://i.instagram.com/api/v1/discover/top_live/",
            headers=headers, timeout=15)
        logger.info(f"top_live status: {r.status_code}")
        if r.status_code == 200:
            data_j = r.json()
            # ranked_items format
            for item in data_j.get("ranked_items",[]):
                b = item.get("broadcast") or item
                u = b.get("broadcast_owner") or item.get("user",{})
                if u.get("username"):
                    add_live(u["username"],u.get("full_name",""),
                        u.get("profile_pic_url",""),
                        b.get("viewer_count",0),
                        b.get("cover_frame_url",""),"explore")
            # items format
            for item in data_j.get("items",[]):
                b = item.get("broadcast")
                u = item.get("user",{})
                if b and u.get("username"):
                    add_live(u["username"],u.get("full_name",""),
                        u.get("profile_pic_url",""),
                        b.get("viewer_count",0),
                        b.get("cover_frame_url",""),"explore")
    except Exception as e:
        logger.error(f"top_live: {e}")

    # 3. Live checklists
    try:
        r = requests.get(
            "https://i.instagram.com/api/v1/live/get_live_checklists/",
            headers=headers, timeout=15)
        logger.info(f"checklists status: {r.status_code}")
        if r.status_code == 200:
            data_j = r.json()
            for item in data_j.get("items",[]):
                b = item.get("broadcast") or item
                u = b.get("broadcast_owner") or item.get("user",{})
                if u.get("username"):
                    add_live(u["username"],u.get("full_name",""),
                        u.get("profile_pic_url",""),
                        b.get("viewer_count",0),
                        b.get("cover_frame_url",""),"suggested")
    except Exception as e:
        logger.error(f"checklists: {e}")

    # 4. Scrape Instagram web explore for lives
    try:
        web_headers = {
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID": "567067343352427"
        }
        r = requests.get(
            "https://www.instagram.com/explore/",
            headers=web_headers, timeout=15)
        if r.status_code == 200 and "broadcast" in r.text:
            # Extract broadcast data from page HTML
            import re
            broadcasts = re.findall(
                r'"broadcast":\{"([^}]+)\}', r.text)
            logger.info(f"Web explore broadcasts: {len(broadcasts)}")
    except Exception as e:
        logger.error(f"web explore: {e}")

    lives.sort(key=lambda x: x.get("viewers",0), reverse=True)
    logger.info(f"Total lives found: {len(lives)}")
    return jsonify({"lives":lives,"count":len(lives)})



# Brazil coordinates for major cities
BRAZIL_CITIES = [
    {"name": "São Paulo",    "lat": -23.5505, "lng": -46.6333},
    {"name": "Rio de Janeiro","lat": -22.9068, "lng": -43.1729},
    {"name": "Brasília",     "lat": -15.7975, "lng": -47.8919},
    {"name": "Salvador",     "lat": -12.9714, "lng": -38.5014},
    {"name": "Fortaleza",    "lat": -3.7172,  "lng": -38.5433},
    {"name": "Belo Horizonte","lat": -19.9191, "lng": -43.9386},
    {"name": "Manaus",       "lat": -3.1190,  "lng": -60.0217},
    {"name": "Curitiba",     "lat": -25.4290, "lng": -49.2671},
    {"name": "Recife",       "lat": -8.0476,  "lng": -34.8770},
    {"name": "Porto Alegre", "lat": -30.0346, "lng": -51.2177},
]

@app.route("/discover/brazil", methods=["POST"])
def discover_brazil_lives():
    """Discover lives from Brazil using location-based search"""
    data = request.json
    if not data: return jsonify({"error":"No data"}),400
    cookie = data.get("cookie","")
    if not cookie: return jsonify({"error":"cookie required"}),400

    ua = "Instagram/269.0.0.18.75 Android (28/9; 420dpi; 1080x1920; samsung; SM-G998B; b0q; qcom; en_US; 567067343352427)"
    headers = {
        "Cookie": cookie,
        "User-Agent": ua,
        "X-IG-App-ID": "567067343352427",
        "X-IG-Capabilities": "3brTvwE=",
        "X-IG-Connection-Type": "WIFI",
        "X-FB-HTTP-Engine": "Liger",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Accept": "*/*"
    }

    lives = []
    seen_users = set()
    seen_locations = set()

    def add_live(username, full_name, pic, viewers, thumb, city):
        if username and username not in seen_users:
            seen_users.add(username)
            lives.append({
                "username": username,
                "full_name": full_name,
                "profile_pic": pic,
                "viewers": viewers,
                "thumbnail": thumb,
                "source": f"Brazil - {city}"
            })

    # Step 1: Search location IDs for each Brazilian city
    location_ids = []
    for city in BRAZIL_CITIES:
        try:
            params = {
                "lat": city["lat"],
                "lng": city["lng"],
                "count": 20,
                "query": city["name"]
            }
            r = requests.get(
                "https://i.instagram.com/api/v1/fbsearch/places/",
                headers=headers, params=params, timeout=10)
            logger.info(f"Places {city['name']}: {r.status_code}")
            if r.status_code == 200:
                items = r.json().get("items", [])
                for item in items[:5]:  # Top 5 locations per city
                    loc = item.get("location", {})
                    loc_id = loc.get("pk") or loc.get("facebook_places_id")
                    if loc_id and loc_id not in seen_locations:
                        seen_locations.add(loc_id)
                        location_ids.append({
                            "id": loc_id,
                            "city": city["name"]
                        })
            time.sleep(0.5)  # Rate limit
        except Exception as e:
            logger.error(f"Places search {city['name']}: {e}")

    logger.info(f"Found {len(location_ids)} location IDs")

    # Step 2: Check each location for active live stories
    for loc in location_ids[:30]:  # Max 30 locations
        try:
            r = requests.get(
                f"https://i.instagram.com/api/v1/locations/{loc['id']}/story/",
                headers=headers, timeout=10)
            if r.status_code == 200:
                data_j = r.json()
                # Check for live broadcast
                broadcast = data_j.get("broadcast")
                if broadcast:
                    owner = broadcast.get("broadcast_owner", {})
                    username = owner.get("username", "")
                    if username:
                        add_live(
                            username,
                            owner.get("full_name", ""),
                            owner.get("profile_pic_url", ""),
                            broadcast.get("viewer_count", 0),
                            broadcast.get("cover_frame_url", ""),
                            loc["city"]
                        )
                # Check reels in location
                reels = data_j.get("reels", {})
                for reel_id, reel in reels.items():
                    b = reel.get("broadcast")
                    if b:
                        u = reel.get("user", {})
                        if u.get("username"):
                            add_live(
                                u["username"],
                                u.get("full_name",""),
                                u.get("profile_pic_url",""),
                                b.get("viewer_count",0),
                                b.get("cover_frame_url",""),
                                loc["city"]
                            )
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"Location story {loc['id']}: {e}")

    # Step 3: Also search by Brazilian hashtags to find live users
    brazil_tags = ["brasil", "brazil", "saopaulo", "riodejaneiro",
                   "brasileiros", "brazilians"]
    for tag in brazil_tags[:3]:
        try:
            r = requests.get(
                f"https://i.instagram.com/api/v1/feed/tag/{tag}/?count=20",
                headers=headers, timeout=10)
            if r.status_code == 200:
                items = r.json().get("items", [])
                # Get unique users from recent posts
                tag_users = []
                for item in items[:10]:
                    user = item.get("user", {})
                    uid = user.get("pk")
                    uname = user.get("username","")
                    if uid and uname and uname not in seen_users:
                        tag_users.append({"id":uid,"username":uname})
                # Check if any are live
                if tag_users:
                    user_ids = ",".join([str(u["id"]) for u in tag_users[:10]])
                    r2 = requests.get(
                        f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_ids}",
                        headers=headers, timeout=10)
                    if r2.status_code == 200:
                        reels = r2.json().get("reels", {})
                        for uid, reel in reels.items():
                            b = reel.get("broadcast")
                            if b:
                                u = reel.get("user",{})
                                add_live(
                                    u.get("username",""),
                                    u.get("full_name",""),
                                    u.get("profile_pic_url",""),
                                    b.get("viewer_count",0),
                                    b.get("cover_frame_url",""),
                                    "Brazil (hashtag)"
                                )
            time.sleep(1)
        except Exception as e:
            logger.error(f"Hashtag {tag}: {e}")

    lives.sort(key=lambda x: x.get("viewers",0), reverse=True)
    logger.info(f"Brazil lives found: {len(lives)}")
    return jsonify({
        "lives": lives,
        "count": len(lives),
        "locations_checked": len(location_ids)
    })



# ── FOLLOWER GRAPH SCANNER ─────────────────────────────────
# Builds a network of accounts to track dynamically
tracked_graph = set()      # all discovered usernames
graph_lock = threading.Lock()
graph_file = "/tmp/graph.json"

def save_graph():
    try:
        with graph_lock:
            data = list(tracked_graph)
        with open(graph_file,'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"save_graph: {e}")

def load_graph():
    global tracked_graph
    try:
        if os.path.exists(graph_file):
            with open(graph_file,'r') as f:
                data = json.load(f)
            with graph_lock:
                tracked_graph = set(data)
            logger.info(f"Loaded graph: {len(tracked_graph)} accounts")
    except Exception as e:
        logger.error(f"load_graph: {e}")

load_graph()

def get_following(user_id, cookie, max_count=200):
    """Get list of accounts a user follows"""
    ua = "Instagram 269.0.0.18.75 Android (28/9; 420dpi; 1080x1920; samsung; SM-G998B; b0q; qcom; en_US; 567067343352427)"
    following = []
    next_max_id = ""
    pages = 0
    while pages < 4:  # Max 4 pages = ~200 accounts
        try:
            params = f"count=50"
            if next_max_id:
                params += f"&max_id={next_max_id}"
            r = requests.get(
                f"https://i.instagram.com/api/v1/friendships/{user_id}/following/?{params}",
                headers={
                    "Cookie": cookie,
                    "User-Agent": ua,
                    "X-IG-App-ID": "567067343352427",
                },
                timeout=15
            )
            if r.status_code != 200:
                logger.warning(f"get_following {user_id}: {r.status_code}")
                break
            data = r.json()
            users = data.get("users", [])
            for u in users:
                uname = u.get("username","")
                if uname:
                    following.append({
                        "username": uname,
                        "user_id": str(u.get("pk","")),
                        "full_name": u.get("full_name",""),
                        "profile_pic": u.get("profile_pic_url",""),
                        "is_verified": u.get("is_verified", False),
                    })
            next_max_id = data.get("next_max_id","")
            if not next_max_id or len(users) == 0:
                break
            pages += 1
            time.sleep(1)
        except Exception as e:
            logger.error(f"get_following page {pages}: {e}")
            break
    return following

def get_user_id_from_cookie(cookie):
    """Get user ID of the logged in account"""
    try:
        ua = "Instagram 269.0.0.18.75 Android (28/9; 420dpi; 1080x1920; samsung; SM-G998B; b0q; qcom; en_US; 567067343352427)"
        r = requests.get(
            "https://i.instagram.com/api/v1/accounts/current_user/?edit=true",
            headers={
                "Cookie": cookie,
                "User-Agent": ua,
                "X-IG-App-ID": "567067343352427",
            },
            timeout=10
        )
        if r.status_code == 200:
            user = r.json().get("user",{})
            return str(user.get("pk","")), user.get("username","")
    except Exception as e:
        logger.error(f"get_user_id: {e}")
    return None, None

def build_follower_graph(cookie, depth=2):
    """
    Build follower graph:
    depth=1: accounts you follow
    depth=2: accounts your followings follow (2nd degree)
    """
    logger.info(f"Building follower graph depth={depth}")

    # Step 1: Get my user ID
    my_id, my_username = get_user_id_from_cookie(cookie)
    if not my_id:
        logger.error("Could not get user ID")
        return

    logger.info(f"Building graph for @{my_username}")

    # Step 2: Get accounts I follow (depth 1)
    my_following = get_following(my_id, cookie, max_count=200)
    logger.info(f"You follow {len(my_following)} accounts")

    new_accounts = set()
    for u in my_following:
        new_accounts.add(u["username"])

    if depth >= 2:
        # Step 3: For each account I follow, get their followings
        for i, user in enumerate(my_following[:50]):  # Limit to 50 for speed
            uid = user.get("user_id","")
            uname = user.get("username","")
            if not uid:
                continue
            try:
                logger.info(f"Getting following of @{uname} ({i+1}/{min(50,len(my_following))})")
                their_following = get_following(uid, cookie, max_count=50)
                for u in their_following:
                    new_accounts.add(u["username"])
                time.sleep(1.5)  # Rate limit
            except Exception as e:
                logger.error(f"Graph depth2 @{uname}: {e}")

    # Add all to tracked graph
    with graph_lock:
        before = len(tracked_graph)
        tracked_graph.update(new_accounts)
        added = len(tracked_graph) - before

    save_graph()
    logger.info(f"Graph built: {len(tracked_graph)} total accounts (+{added} new)")

    # Trigger a live scan with new graph
    if registered_accounts:
        t = threading.Thread(
            target=scan_graph_accounts,
            daemon=True
        )
        t.start()

def scan_graph_accounts():
    """Scan all graph accounts for live status"""
    with graph_lock:
        accounts = list(tracked_graph)

    if not accounts:
        logger.warning("Graph empty, nothing to scan")
        return

    cookies = list(registered_accounts.keys())
    if not cookies:
        return

    logger.info(f"Scanning {len(accounts)} graph accounts for live")
    ua = "Instagram 269.0.0.18.75 Android (28/9; 420dpi; 1080x1920; samsung; SM-G998B; b0q; qcom; en_US; 567067343352427)"

    cookie_idx = 0
    found = 0

    for username in accounts:
        try:
            cookie = cookies[cookie_idx % len(cookies)]
            cookie_idx += 1

            r = requests.get(
                f"https://i.instagram.com/api/v1/users/{username}/usernameinfo/",
                headers={
                    "Cookie": cookie,
                    "User-Agent": ua,
                    "X-IG-App-ID": "567067343352427",
                },
                timeout=8
            )

            if r.status_code == 200:
                user = r.json().get("user", {})
                is_live = user.get("is_live", False)
                if is_live:
                    uid = user.get("pk","")
                    viewers = 0
                    thumbnail = ""
                    if uid:
                        try:
                            r2 = requests.get(
                                f"https://i.instagram.com/api/v1/feed/user/{uid}/story/",
                                headers={
                                    "Cookie": cookie,
                                    "User-Agent": ua,
                                    "X-IG-App-ID": "567067343352427",
                                },
                                timeout=8
                            )
                            if r2.status_code == 200:
                                b = r2.json().get("broadcast",{})
                                viewers = b.get("viewer_count",0)
                                thumbnail = b.get("cover_frame_url","")
                        except: pass

                    with scan_lock:
                        live_cache[username] = {
                            "username": username,
                            "full_name": user.get("full_name",""),
                            "profile_pic": user.get("profile_pic_url",""),
                            "viewers": viewers,
                            "thumbnail": thumbnail,
                            "timestamp": time.time(),
                        }
                    found += 1
                    logger.info(f"LIVE @{username} viewers={viewers}")
                else:
                    with scan_lock:
                        live_cache.pop(username, None)

            elif r.status_code == 429:
                logger.warning("Rate limited, sleeping 30s")
                time.sleep(30)

            time.sleep(0.5 + random.uniform(0, 0.3))

        except Exception as e:
            logger.error(f"scan @{username}: {e}")

    logger.info(f"Graph scan done. Found {found} live")

def auto_graph_scanner():
    """Continuously rebuild graph and scan"""
    while True:
        if registered_accounts:
            cookie = list(registered_accounts.keys())[0]
            # Rebuild graph every 30 mins
            build_follower_graph(cookie, depth=2)
            # Scan immediately after rebuild
            scan_graph_accounts()
        time.sleep(1800)  # 30 minutes

# Start auto graph scanner
graph_thread = threading.Thread(target=auto_graph_scanner, daemon=True)
graph_thread.start()

@app.route("/graph/build", methods=["POST"])
def api_build_graph():
    """Manually trigger graph build"""
    data = request.json or {}
    cookie = data.get("cookie","")
    if not cookie:
        # Use registered cookie
        if not registered_accounts:
            return jsonify({"error":"No accounts registered"}), 400
        cookie = list(registered_accounts.keys())[0]

    depth = int(data.get("depth", 2))

    t = threading.Thread(
        target=build_follower_graph,
        args=(cookie, depth),
        daemon=True
    )
    t.start()

    return jsonify({
        "status": "building",
        "message": f"Building follower graph depth={depth}",
        "current_size": len(tracked_graph)
    })

@app.route("/graph/status")
def api_graph_status():
    with graph_lock:
        size = len(tracked_graph)
        sample = list(tracked_graph)[:10]
    with scan_lock:
        live_count = len(live_cache)
    return jsonify({
        "total_tracked": size,
        "live_now": live_count,
        "sample": sample,
    })

@app.route("/graph/add", methods=["POST"])
def api_graph_add():
    """Manually add usernames to track"""
    data = request.json or {}
    usernames = data.get("usernames", [])
    if isinstance(usernames, str):
        usernames = [u.strip() for u in usernames.split(",")]
    with graph_lock:
        before = len(tracked_graph)
        for u in usernames:
            if u.strip():
                tracked_graph.add(u.strip().replace("@",""))
        added = len(tracked_graph) - before
    save_graph()
    return jsonify({"added": added, "total": len(tracked_graph)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
