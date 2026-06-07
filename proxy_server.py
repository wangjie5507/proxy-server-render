"""tools/proxy_server.py — 内容水龙头 API 代理 + Stripe 自动续费。

一键部署到 Render.com (免费). 客户应用不接触真实 API Key.
同时充当 Key Server + Pixabay/Pexels/HeyGen 中转加速 + Stripe webhook.
"""
import os, requests, json, base64, time as time_module
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import jwt

app = Flask(__name__)

MASTER_KEY = os.environ.get("MASTER_KEY", "change-me")
API_KEYS = {
    "STEP_PLAN_API_KEY": os.environ.get("STEP_PLAN_API_KEY", ""),
    "DEEPSEEK_API_KEY":  os.environ.get("DEEPSEEK_API_KEY", ""),
    "MOONSHOT_API_KEY":  os.environ.get("MOONSHOT_API_KEY", ""),
    "PIXABAY_API_KEY":   os.environ.get("PIXABAY_API_KEY", "47648800-ed68747d593dab76101c57a82"),
    "PEXELS_API_KEY":    os.environ.get("PEXELS_API_KEY", "kX2kERzIBkk2jcOAGMqN9zMxLYlMiglj9eLYctjWqY1MhOdoYHobqgW2"),
    "HEYGEN_API_KEY":   os.environ.get("HEYGEN_API_KEY", ""),
    "KLING_ACCESS_KEY": os.environ.get("KLING_ACCESS_KEY", ""),
    "KLING_SECRET_KEY": os.environ.get("KLING_SECRET_KEY", ""),
}
HEYGEN_BASE = "https://api.heygen.com"
KLING_BASE = "https://api.klingai.com"
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

OPENAI_PROXY = {
    "step_plan": "https://api.stepfun.com/step_plan/v1",
    "deepseek":  "https://api.deepseek.com",
    "moonshot":  "https://api.moonshot.cn/v1",
}

@app.route("/keys.json")
def serve_keys():
    provided = request.args.get("key", "")
    if provided != MASTER_KEY:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({k: v for k, v in API_KEYS.items() if v})

@app.route("/v1/chat/completions", methods=["POST"])
def proxy_chat():
    provider = request.args.get("provider", "step_plan")
    target = OPENAI_PROXY.get(provider, OPENAI_PROXY["step_plan"])
    key_name = "STEP_PLAN_API_KEY" if provider == "step_plan" else provider.upper() + "_API_KEY"
    key = API_KEYS.get(key_name, API_KEYS.get("STEP_PLAN_API_KEY", ""))
    if not key:
        return jsonify({"error": "provider not configured"}), 500
    body = request.get_json(force=True, silent=True) or {}
    resp = requests.post(f"{target}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body, timeout=60)
    return jsonify(resp.json()), resp.status_code

@app.route("/pixabay/videos")
def proxy_pixabay_videos():
    resp = requests.get("https://pixabay.com/api/videos/",
        params={**request.args, "key": API_KEYS["PIXABAY_API_KEY"]}, timeout=15)
    return jsonify(resp.json()), resp.status_code

@app.route("/pixabay/images")
def proxy_pixabay_images():
    resp = requests.get("https://pixabay.com/api/",
        params={**request.args, "key": API_KEYS["PIXABAY_API_KEY"]}, timeout=15)
    return jsonify(resp.json()), resp.status_code

@app.route("/pexels/videos")
def proxy_pexels_videos():
    resp = requests.get("https://api.pexels.com/videos/search",
        headers={"Authorization": API_KEYS["PEXELS_API_KEY"]}, params=request.args, timeout=15)
    return jsonify(resp.json()), resp.status_code

@app.route("/pexels/photos")
def proxy_pexels_photos():
    resp = requests.get("https://api.pexels.com/v1/search",
        headers={"Authorization": API_KEYS["PEXELS_API_KEY"]}, params=request.args, timeout=15)
    return jsonify(resp.json()), resp.status_code

# HeyGen 中转代理
@app.route("/heygen/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy_heygen(subpath):
    key = API_KEYS.get("HEYGEN_API_KEY", "")
    if not key:
        return jsonify({"error": "HeyGen API key not configured"}), 500
    method = request.method
    url = f"{HEYGEN_BASE}/{subpath}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    params = request.args.to_dict() if method == "GET" else None
    json_body = None
    if method in ("POST", "PUT"):
        json_body = request.get_json(force=True, silent=True) or {}
    resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=60)
    return jsonify(resp.json()), resp.status_code

# Kling AI 中转代理 (JWT 鉴权)
def _kling_token():
    ak = API_KEYS.get("KLING_ACCESS_KEY", "")
    sk = API_KEYS.get("KLING_SECRET_KEY", "")
    if not ak or not sk:
        return None
    payload = {"iss": ak, "exp": int(time_module.time()) + 1800, "nbf": int(time_module.time()) - 5}
    return jwt.encode(payload, sk, algorithm="HS256")

@app.route("/kling/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy_kling(subpath):
    token = _kling_token()
    if not token:
        return jsonify({"error": "Kling API keys not configured"}), 500
    method = request.method
    url = f"{KLING_BASE}/{subpath}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = request.args.to_dict() if method == "GET" else None
    json_body = None
    if method in ("POST", "PUT"):
        json_body = request.get_json(force=True, silent=True) or {}
    resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=120)
    return jsonify(resp.json()), resp.status_code

# Stripe 自动续费: 收款后自动生成新授权码
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    try:
        event = json.loads(payload)
    except Exception:
        return jsonify({"error": "invalid json"}), 400
    if event.get("type") != "checkout.session.completed":
        return jsonify({"status": "ignored"}), 200
    email = event.get("data",{}).get("object",{}).get("customer_details",{}).get("email","unknown")
    plan = event.get("data",{}).get("object",{}).get("metadata",{}).get("plan","pro")
    days = int(event.get("data",{}).get("object",{}).get("metadata",{}).get("days","365"))
    payload_data = {
        "plan": plan, "issued": datetime.now().isoformat(),
        "expires": (datetime.now() + timedelta(days=days)).isoformat(),
        "user_id": email,
        "key_server_url": f"{request.host_url}keys.json?key={MASTER_KEY}",
    }
    new_key = base64.b64encode(json.dumps(payload_data).encode()).decode()
    print(f"[Stripe] {email} {plan}x{days}d -> license generated")
    return jsonify({"status":"ok","license_key":new_key,"plan":plan,"days":days,"email":email}), 200

@app.route("/")
def health():
    return jsonify({"status":"ok","providers":list(OPENAI_PROXY.keys())+["heygen","kling","pixabay","pexels"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
