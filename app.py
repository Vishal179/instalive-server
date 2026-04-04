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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
