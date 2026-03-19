from flask import Flask, request, jsonify, send_file
import subprocess, os, threading, uuid, time, logging, requests
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
RECORDINGS_DIR = "/tmp/recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
active_jobs = {}
monitored_accounts = {}
monitoring_active = False
NTFY_TOPIC = "Inslive-jhwalker"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

def send_notification(title, message, priority="high", tags="movie_camera"):
    try:
        requests.post(NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Content-Type": "text/plain"
            }, timeout=10)
        logger.info(f"Notification sent: {title}")
    except Exception as e:
        logger.error(f"Notification failed: {e}")

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
    if not username or not cookie: return jsonify({"error": "username and cookie required"}), 400
    for jid, job in active_jobs.items():
        if job["username"]==username and job["status"]=="recording":
            return jsonify({"job_id": jid, "status": "already_recording"})
    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=record_live, args=(job_id, username, cookie, from_start))
    thread.daemon = True
    thread.start()
    active_jobs[job_id] = {"status": "starting", "username": username,
                           "started_at": time.time(), "file": None, "error": None, "process": None}
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
    monitored_accounts[account_id] = {"cookie": cookie, "last_check": 0}
    if not monitoring_active:
        monitoring_active = True
        t = threading.Thread(target=monitor_loop)
        t.daemon = True
        t.start()
        logger.info("Monitoring started")
        send_notification("InstaLive Server", "Monitoring started for your accounts", priority="low", tags="eyes")
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
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    response = {"job_id": job_id, "status": job["status"],
                "username": job["username"], "error": job["error"]}
    if job["status"]=="done" and job["file"] and os.path.exists(job["file"]):
        response["file_size"] = os.path.getsize(job["file"])
        response["ready_to_download"] = True
    return jsonify(response)

@app.route("/jobs", methods=["GET"])
def list_jobs():
    return jsonify({"jobs": [{"job_id": jid, "username": j["username"],
                              "status": j["status"]} for jid,j in active_jobs.items()]})

@app.route("/download/<job_id>", methods=["GET"])
def download_file(job_id):
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job["status"]!="done": return jsonify({"error": "Not finished", "status": job["status"]}), 202
    if not job["file"] or not os.path.exists(job["file"]): return jsonify({"error": "File not found"}), 404
    return send_file(job["file"], as_attachment=True,
                     download_name=f"{job['username']}_live.mp4", mimetype="video/mp4")

@app.route("/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job.get("process"):
        try: job["process"].terminate(); job["status"]="stopped"
        except: pass
    return jsonify({"status": "stopped"})

def monitor_loop():
    global monitoring_active
    logger.info("Monitor loop started")
    while monitoring_active and monitored_accounts:
        for account_id, account in list(monitored_accounts.items()):
            try:
                check_account_for_live(account_id, account["cookie"])
            except Exception as e:
                logger.error(f"Monitor error for {account_id}: {e}")
        time.sleep(30)
    monitoring_active = False
    logger.info("Monitor loop stopped")

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
            logger.warning(f"Reels tray {resp.status_code} for {account_id}")
            return
        data = resp.json()
        tray = data.get("tray", [])
        for item in tray:
            broadcast = item.get("broadcast")
            if not broadcast: continue
            user = item.get("user", {})
            username = user.get("username", "")
            if not username: continue
            already = any(j["username"]==username and j["status"] in ["recording","starting"]
                         for j in active_jobs.values())
            if already: continue
            viewer_count = broadcast.get("viewer_count", 0)
            logger.info(f"LIVE: @{username} viewers={viewer_count}")
            # Send notification immediately
            send_notification(
                f"🔴 @{username} is LIVE!",
                f"Recording started automatically\n👥 {viewer_count} viewers watching",
                priority="urgent",
                tags="red_circle,movie_camera"
            )
            # Start recording immediately
            job_id = str(uuid.uuid4())[:8]
            t = threading.Thread(target=record_live, args=(job_id, username, cookie, True))
            t.daemon = True
            t.start()
            active_jobs[job_id] = {
                "status": "starting", "username": username,
                "started_at": time.time(), "file": None,
                "error": None, "process": None, "detected_by": "monitor"
            }
            logger.info(f"Auto-started job {job_id} for @{username}")
    except Exception as e:
        logger.error(f"check_account_for_live error: {e}")

def record_live(job_id, username, cookie, from_start=True):
    output_file = os.path.join(RECORDINGS_DIR, f"{job_id}_{username}.mp4")
    active_jobs[job_id]["status"] = "recording"
    active_jobs[job_id]["file"] = output_file
    try:
        cmd = ["yt-dlp",
               "--add-header", f"Cookie:{cookie}",
               "--add-header", "User-Agent:Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/105.0 Mobile Safari/537.36",
               "--add-header", "X-IG-App-ID:936619743392459",
               "-f", "best", "-o", output_file,
               "--no-part", "--retries", "10", "--fragment-retries", "10"]
        if from_start: cmd.append("--live-from-start")
        cmd.append(f"https://www.instagram.com/{username}/live/")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        active_jobs[job_id]["process"] = process
        for line in process.stdout:
            if line.strip(): logger.info(f"[{job_id}] {line.strip()}")
        process.wait()
        if process.returncode==0 and os.path.exists(output_file) and os.path.getsize(output_file)>0:
            active_jobs[job_id]["status"] = "done"
            size_mb = os.path.getsize(output_file) / (1024*1024)
            # Notify recording done
            send_notification(
                f"✅ @{username} recording saved!",
                f"Live recording complete\n📁 Size: {size_mb:.1f} MB\nOpen app to download",
                priority="high",
                tags="white_check_mark,floppy_disk"
            )
        else:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = f"Exit code {process.returncode}"
            send_notification(
                f"⚠️ Recording failed - @{username}",
                f"Could not record live. Live may have ended too quickly.",
                priority="default",
                tags="warning"
            )
    except Exception as e:
        active_jobs[job_id]["status"] = "failed"
        active_jobs[job_id]["error"] = str(e)
    threading.Timer(3600, lambda: cleanup_job(job_id)).start()

def cleanup_job(job_id):
    if job_id in active_jobs:
        job = active_jobs[job_id]
        if job.get("file") and os.path.exists(job["file"]):
            try: os.remove(job["file"])
            except: pass
        del active_jobs[job_id]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
