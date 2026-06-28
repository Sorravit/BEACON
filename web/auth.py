"""Authentication: token gate + login page + HTTP middleware."""

import os

from fastapi import Request

_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()


def _login_page_html() -> str:
    return '''<!DOCTYPE html><html><head><title>BEACON Login</title>
<style>*{box-sizing:border-box}body{background:#0f0f17;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:system-ui,sans-serif;}
.box{background:#1a1a2e;padding:2.5rem;border-radius:16px;width:320px;box-shadow:0 8px 32px #0006;}
h2{color:#cdd6f4;margin:0 0 1.5rem;text-align:center;font-size:1.4rem;}  
input{width:100%;padding:.7rem 1rem;background:#252540;border:1px solid #44446a;border-radius:8px;color:#cdd6f4;font-size:14px;margin-bottom:1rem;outline:none;}
input:focus{border-color:#6c63ff;}
button{width:100%;padding:.75rem;background:#6c63ff;border:none;border-radius:8px;color:#fff;font-size:15px;cursor:pointer;font-weight:600;}
button:hover{background:#5a52d5;}
.err{color:#f38ba8;font-size:13px;text-align:center;margin-top:.5rem;display:none;}</style></head>
<body><div class="box"><h2>🤖 BEACON</h2>
<form onsubmit="login(event)">
<input type="password" id="tok" placeholder="Access token" autofocus>
<button type="submit">Login</button>
<p class="err" id="err">Invalid token — try again</p>
</form></div>
<script>
async function login(e){
  e.preventDefault();
  const tok=document.getElementById("tok").value.trim();
  if(!tok)return;
  const r=await fetch("/health",{headers:{"Authorization":"Bearer "+tok}});
  if(r.ok){document.cookie="auth_token="+encodeURIComponent(tok)+";path=/;max-age=86400";location.reload();}
  else{document.getElementById("err").style.display="block";}}
</script></body></html>'''


async def auth_middleware(request: Request, call_next):
    # Auth disabled if no token configured (dev mode)
    if not _AUTH_TOKEN:
        return await call_next(request)
    # Always allow health check (used by login page to verify token)
    skip_paths = ["/health", "/static", "/favicon"]
    if any(request.url.path.startswith(p) for p in skip_paths):
        return await call_next(request)
    # Extract token from Authorization header, cookie, or query param
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get("auth_token", "").strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if token == _AUTH_TOKEN:
        return await call_next(request)
    # Unauthorized — return login page for browser, JSON for API
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        from fastapi.responses import HTMLResponse as _HR
        return _HR(content=_login_page_html(), status_code=401)
    from fastapi.responses import JSONResponse as _JR
    return _JR(status_code=401, content={"error": "Unauthorized — set AUTH_TOKEN in .env"})
