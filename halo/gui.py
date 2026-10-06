"""`halo gui`: a small local control panel (stdlib only, opens in the browser).

Switch hybrid / frontier-only, see Ollama + hardware status, pick models, see savings and
run `tune` / speed checks. Binds to 127.0.0.1 by default; every state-changing request needs
a per-process token embedded in the page, so other websites cannot flip settings.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, catalog, config, doctor, integration, ledger, llm, tasks, vendors

TOKEN = secrets.token_urlsafe(16)
_job = {"name": None, "log": [], "running": False, "started": 0}
_lock = threading.Lock()


def _mcp_registered() -> bool:
    try:
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as fh:
            return "halo" in (json.load(fh).get("mcpServers") or {})
    except (OSError, ValueError):
        return False


def _login_path() -> None:
    """A panel started from the macOS HALO app (or a desktop launcher) gets a bare PATH
    (/usr/bin:/bin), so agent CLIs installed with npm/Homebrew look missing. Borrow the PATH
    of the user's login shell, plus the usual install dirs."""
    if sys.platform == "win32":
        return
    extra = []
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    try:
        out = subprocess.run([shell, "-ilc", 'printf "\\n__P__%s" "$PATH"'], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL).stdout
        extra = out.rsplit("__P__", 1)[1].strip().split(":") if "__P__" in out else []
    except (OSError, subprocess.SubprocessError):
        pass
    extra += [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    have = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(have + [d for d in extra if d]))




def _council_choices(installed: list[str]) -> list[str]:
    return list(llm.CLI_WORKERS) + installed


HANDOFF_AGENTS = tuple(vendors.names()) + ("claude:haiku", "claude:sonnet")


def _recent_handoffs(records: list[dict], n: int = 6) -> list[dict]:
    keep = ("ts", "status", "model", "seconds", "cloud_in", "cloud_out", "rounds")
    return [{k: r.get(k) for k in keep} for r in records if r.get("kind") == "handoff"][-n:][::-1]


def status() -> dict:
    cfg = config.load()
    try:
        tags = llm.raw(cfg, "/api/tags", timeout=3).get("models", [])
        installed = sorted(m["name"] for m in tags)
        ollama = True
    except llm.LocalModelError:
        installed, ollama = [], False
    records = ledger.read()
    s = ledger.summary(records)
    t = s["total"]
    hw = doctor.accelerator()
    cpu = hw["kind"] == "cpu"
    recs = {m.name for m in catalog.recommended(hw["gib"], cpu)}
    choices = [{"name": m.name, "size": m.size_gib, "good_at": m.good_at, "note": m.note,
                "thai": m.thai, "moe": m.moe, "tested": m.tested,
                "installed": m.name in installed, "recommended": m.name in recs}
               for m in catalog.fitting(hw["gib"], cpu)]
    return {
        "version": __version__,
        "mode": cfg.mode,
        "ollama": {"ok": ollama, "url": cfg.ollama_url},
        "hardware": hw,
        "catalog": choices,
        "models": {"model": cfg.model, "code_model": cfg.code_model,
                   "fallback_models": cfg.fallback_models, "installed": installed},
        "claude": dict(integration.status(), mcp_registered=_mcp_registered()),
        "agents": [dict(a, name=n, hint=getattr(vendors.get(n), "install", ""))
                   for n, a in integration.status()["agents"].items()],
        "vendors": vendors.status(cfg),
        "council": {"members": tasks.council_models(cfg),
                    "choices": _council_choices(installed)},
        "handoff": {"agent": cfg.handoff_agent, "rounds": cfg.handoff_rounds,
                    "choices": list(HANDOFF_AGENTS), "recent": _recent_handoffs(records)},
        "stats": {"tasks": t.get("tasks", 0), "ok": t.get("ok", 0) + t.get("passed", 0),
                  "local_tokens": t.get("local_in", 0) + t.get("local_out", 0),
                  "saved_upper_bound": s["frontier_saved_est"]},
        "job": {k: _job[k] for k in ("name", "running")} | {"log": _job["log"][-40:]},
    }


def _start_job(name: str, fn):
    with _lock:
        if _job["running"]:
            return False
        _job.update(name=name, log=[], running=True, started=time.time())

    def log(msg):
        _job["log"].append(str(msg))

    def run():
        try:
            fn(log)
        except Exception as e:  # show, never crash the panel
            log(f"error: {type(e).__name__}: {e}")
        finally:
            _job["running"] = False
            log("done.")

    threading.Thread(target=run, daemon=True).start()
    return True


def _tune(log):
    from . import tune
    tune.run(config.load(), progress=log)


def _pull_job(names):
    def run(log):
        cfg = config.load()
        for n in names:
            log(f"downloading {n} ...")
            if not llm.pull(cfg, n, progress=log):
                log(f"{n}: failed")
        log("Tip: press 'Auto-pick best for this machine' to measure them and assign roles.")
    return run


def _doctor(log):
    lines, _ = doctor.run(config.load(), bench=True)
    for line in lines:
        log(line)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            # Initial state is embedded so the page never flashes empty cards.
            init = json.dumps(status()).replace("</", "<\\/")
            self._send(PAGE.replace("__TOKEN__", TOKEN).replace("__INIT__", init), "text/html")
        elif self.path == "/api/status":
            self._send(status())
        else:
            self._send({"error": "not found"}, code=404)

    def do_POST(self):
        if self.headers.get("X-Halo-Token") != TOKEN:
            self._send({"error": "bad token"}, code=403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/mode":
            try:
                integration.set_mode(body.get("mode", ""))
            except ValueError as e:
                self._send({"error": str(e)}, code=400)
                return
            self._send(status())
        elif self.path == "/api/models":
            cfg = config.load()
            installed = set(status()["models"]["installed"])
            for key in ("model", "code_model"):
                val = body.get(key)
                if val is not None and (val == "" or val in installed):
                    setattr(cfg, key, val)
            if isinstance(body.get("fallback_models"), list):
                cfg.fallback_models = [m for m in body["fallback_models"] if m in installed]
            config.save(cfg)
            self._send(status())
        elif self.path == "/api/agent":
            name, action = body.get("name"), body.get("action")
            if name not in integration.AGENTS or action not in ("add", "remove"):
                self._send({"error": "bad agent or action"}, code=400)
                return
            if action == "add":
                rc = integration.install([name], config.load().mode)
                if rc:
                    self._send({"error": f"could not register HALO with {name} (is it "
                                         f"installed and logged in?)"}, code=500)
                    return
            else:
                integration.uninstall(name)
            self._send(status())
        elif self.path == "/api/council":
            cfg = config.load()
            choices = set(_council_choices(status()["models"]["installed"]))
            members = [m for m in body.get("members", []) if m in choices]
            if not members:
                self._send({"error": "pick at least one council member"}, code=400)
                return
            cfg.council_models = members
            config.save(cfg)
            self._send(status())
        elif self.path == "/api/handoff":
            cfg = config.load()
            agent, rounds = body.get("agent"), body.get("rounds")
            if agent not in HANDOFF_AGENTS or not isinstance(rounds, int) or not 1 <= rounds <= 5:
                self._send({"error": "pick an agent and 1-5 rounds"}, code=400)
                return
            cfg.handoff_agent, cfg.handoff_rounds = agent, rounds
            config.save(cfg)
            self._send(status())
        elif self.path == "/api/pull":
            allowed = {m.name for m in catalog.CATALOG}
            names = [n for n in body.get("names", []) if n in allowed]
            if not names:
                self._send({"error": "nothing selected"}, code=400)
                return
            ok = _start_job("pull", _pull_job(names))
            self._send({"started": ok}, code=200 if ok else 409)
        elif self.path in ("/api/tune", "/api/doctor"):
            ok = _start_job(self.path[5:], _tune if self.path == "/api/tune" else _doctor)
            self._send({"started": ok}, code=200 if ok else 409)
        else:
            self._send({"error": "not found"}, code=404)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    _login_path()
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print(f"HALO control panel: {url}  (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost"):
        print("  warning: reachable from other machines on this network")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HALO Control</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#626b76;--line:#e3e6ea;--accent:#2f6fde;
--accent-soft:#e8f0fd;--good:#1d8a4e;--bad:#c23b31;--warn:#a86b00;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e22;--text:#e7e9ec;--muted:#98a1ab;
--line:#2c3137;--accent:#6b9cf0;--accent-soft:#1f2a3d;--good:#4cc284;--bad:#ef6b61;--warn:#e0a53a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:880px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:20px}
h1{font-size:22px;margin:0;letter-spacing:.2px}header span{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.sel3{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px;font-weight:600}
.wide{grid-column:1/-1}
.seg{display:flex;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:4px;gap:4px}
.seg button{flex:1;border:0;background:transparent;color:var(--text);padding:12px 10px;border-radius:7px;
font:inherit;font-weight:600;cursor:pointer}
.seg button small{display:block;font-weight:400;color:var(--muted);font-size:12.5px;margin-top:2px}
.seg button.on{background:var(--accent);color:#fff}.seg button.on small{color:#e6eefc}
.note{color:var(--muted);font-size:13px;margin:10px 0 0}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}.row b{font-weight:500;text-align:right;overflow-wrap:anywhere}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.ok{background:var(--good)}.no{background:var(--bad)}
.big{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.stat{display:flex;gap:22px;flex-wrap:wrap}.stat div span{display:block;color:var(--muted);font-size:12.5px}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 4px}
select{width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.btn{border:1px solid var(--line);background:var(--bg);color:var(--text);padding:8px 14px;border-radius:8px;
font:inherit;cursor:pointer}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn:disabled{opacity:.5;cursor:default}
.cat{width:100%;border-collapse:collapse;font-size:14px}
.cat td{padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.cat tr:last-child td{border-bottom:0}.cat .nm{font-family:var(--mono);font-size:13px;overflow-wrap:anywhere}
.cat .sub{color:var(--muted);font-size:12.5px}.tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:99px;
border:1px solid var(--line);margin:2px 4px 0 0;color:var(--muted)}.tag.t{border-color:var(--good);color:var(--good)}
.cat input{width:17px;height:17px;margin-top:2px}
pre{font:12.5px/1.45 var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;
max-height:260px;overflow:auto;white-space:pre-wrap;margin:10px 0 0}
</style></head><body><main>
<header><h1>HALO</h1><span id="ver"></span></header>
<div class="grid">
 <section class="card wide"><h2>Mode</h2>
  <div class="seg">
   <button id="m-hybrid" onclick="setMode('hybrid')">Hybrid<small>Routine work goes to the local model</small></button>
   <button id="m-frontier" onclick="setMode('frontier')">Frontier only<small>HALO off; the frontier model does everything</small></button>
  </div>
  <p class="note" id="modenote">Takes full effect in new agent sessions.</p>
 </section>
 <section class="card"><h2>Local model server</h2><div id="sys"></div></section>
 <section class="card"><h2>Agents</h2><div id="agents"></div>
  <p class="note">Add = the agent gets HALO's tools (local delegation in Hybrid; council and handoff in both modes). Takes effect in new sessions.</p></section>
 <section class="card wide"><h2>Council (frontier + frontier)</h2>
  <p class="note" style="margin:0 0 8px">Who answers when an agent asks the council (halo_council / <code>halo council</code>) for a second opinion. In Frontier only mode local models are skipped.</p>
  <div id="council"></div>
  <div class="btns"><button class="btn primary" onclick="saveCouncil()">Save council</button></div>
 </section>
 <section class="card wide"><h2>Handoff (frontier → frontier)</h2>
  <p class="note" style="margin:0 0 8px">Who takes a whole sub-task when an agent hands one over (halo_handoff / <code>halo handoff</code>). It works in a copy of the project on its own plan's quota; only changes inside the given sector come back. Secrets (.env, keys) are not sent; the rest of the project is.</p>
  <div class="sel3">
   <div><label for="h-agent">Default agent</label><select id="h-agent"></select></div>
   <div><label for="h-rounds">Repair rounds</label><select id="h-rounds"><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
  </div>
  <div class="btns"><button class="btn primary" onclick="saveHandoff()">Save handoff</button></div>
  <div id="handoffs"></div>
 </section>
 <section class="card wide"><h2>Savings</h2><div class="stat" id="stats"></div>
  <p class="note">Local tokens are measured. The frontier figure is an upper bound (it assumes the frontier would have read the whole input).</p></section>
 <section class="card wide"><h2>Download local models</h2>
  <p class="note" style="margin:0 0 8px">Models that fit this machine. ★ = recommended. Tick the ones you want and press Download.</p>
  <div id="cat"></div>
  <div class="btns"><button class="btn primary" id="b-pull" onclick="pullSel()">Download selected</button>
   <button class="btn" onclick="tickRec()">Select recommended</button></div>
 </section>
 <section class="card wide"><h2>Models in use</h2>
  <div class="sel3">
   <div><label for="s-model">Digest / ask</label><select id="s-model"></select></div>
   <div><label for="s-code">Code</label><select id="s-code"></select></div>
   <div><label for="s-fb">Fallback for code</label><select id="s-fb"></select></div>
  </div>
  <div class="btns"><button class="btn" onclick="saveModels()">Save models</button>
   <button class="btn primary" id="b-tune" onclick="job('tune')">Auto-pick best for this machine</button>
   <button class="btn" id="b-doc" onclick="job('doctor')">Check speed</button></div>
  <p class="note">Auto-pick tests every installed model that fits your hardware on a log-reading task and three coding tasks, then saves the best. Takes a few minutes per model.</p>
  <pre id="log" hidden></pre>
 </section>
</div></main>
<script>
const T="__TOKEN__";let S=null,councilDirty=false;
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const row=(k,v)=>`<div class="row"><span>${k}</span><b>${v}</b></div>`;
const dot=ok=>`<span class="dot ${ok?"ok":"no"}"></span>`;
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"X-Halo-Token":T,"Content-Type":"application/json"},body:JSON.stringify(body||{})});return r.json()}
function opts(sel,list,val,blank){sel.innerHTML=(blank?`<option value="">${blank}</option>`:"")+list.map(m=>`<option ${m===val?"selected":""}>${esc(m)}</option>`).join("")}
function render(s){S=s;$("ver").textContent="v"+s.version;
 $("m-hybrid").classList.toggle("on",s.mode==="hybrid");$("m-frontier").classList.toggle("on",s.mode==="frontier");
 const h=s.hardware;
 $("sys").innerHTML=row("Ollama",dot(s.ollama.ok)+(s.ollama.ok?"running":"not reachable"))+row("URL",esc(s.ollama.url))
  +row("Hardware",esc(h.detail))+row("Model budget",h.gib.toFixed(0)+" GiB");
 $("agents").innerHTML=s.agents.map(a=>`<div class="row"><span>${dot(a.enabled&&a.cli)}${esc(a.title)}</span>`
  +(!a.cli?`<span class="sub" title="install it, log in once, then reload">not installed — <code>${esc(a.hint)}</code></span>`
   :a.enabled?`<button class="btn" onclick="agent('${a.name}','remove')">Remove</button>`
   :`<button class="btn primary" onclick="agent('${a.name}','add')">Add</button>`)+`</div>`).join("");
 const on=s.agents.filter(a=>a.enabled&&a.cli).map(a=>a.title);
 $("modenote").textContent=on.length?"Takes full effect in new "+on.join(" / ")+" sessions."
  :"No agent has HALO yet — add one under Agents.";
 const vend=Object.fromEntries(s.vendors.map(v=>[v.name,v]));
 if(!councilDirty){
  const box=m=>{const cloud=!!vend[m],dis=cloud&&!vend[m].installed;
   return `<label class="row" style="justify-content:flex-start"><input type="checkbox" value="${esc(m)}" ${s.council.members.includes(m)?"checked":""} ${dis?"disabled":""}>
   ${esc(m)} <span class="sub">${cloud?esc(vend[m].brand)+(dis?" (not installed)":""):"local"}</span></label>`};
  const cloud=s.council.choices.filter(m=>vend[m]),local=s.council.choices.filter(m=>!cloud.includes(m));
  const nl=local.filter(m=>s.council.members.includes(m)).length;
  $("council").innerHTML=cloud.map(box).join("")+(s.mode==="frontier"||!local.length?"":
   `<details ${nl?"open":""}><summary class="sub" style="cursor:pointer;padding:6px 0">Local models (${nl} chosen, Hybrid only)</summary>${local.map(box).join("")}</details>`)}
 const hf=s.handoff;
 if(!document.activeElement||!["h-agent","h-rounds"].includes(document.activeElement.id)){
  $("h-agent").innerHTML=hf.choices.map(m=>{const ok=!!vend[m.split(":")[0]]?.installed;
   return `<option value="${esc(m)}" ${m===hf.agent?"selected":""} ${ok?"":"disabled"}>${esc(m)}${ok?"":" (not installed)"}</option>`}).join("");
  $("h-rounds").value=String(hf.rounds)}
 $("handoffs").innerHTML=hf.recent.length?`<p class="sub" style="margin:10px 0 4px">Recent handoffs</p>`+hf.recent.map(r=>
  row(`${dot(r.status==="passed"||r.status==="done")}${new Date(r.ts*1000).toLocaleString()} · ${esc(r.model)}`,
   `${esc(r.status)} · ${r.rounds??"?"} round(s) · ${Math.round(r.seconds)}s · ${Number((r.cloud_in||0)+(r.cloud_out||0)).toLocaleString()} tok on its plan`)).join("")
  :`<p class="sub" style="margin:10px 0 0">No handoffs yet.</p>`;
 const st=s.stats,n=x=>Number(x).toLocaleString();
 $("stats").innerHTML=`<div><div class="big">${n(st.tasks)}</div><span>tasks delegated (${n(st.ok)} ok)</span></div>
  <div><div class="big">${n(st.local_tokens)}</div><span>local tokens (free)</span></div>
  <div><div class="big">≤ ${n(st.saved_upper_bound)}</div><span>frontier tokens saved</span></div>`;
 const m=s.models,inst=m.installed;
 if(!document.activeElement||document.activeElement.tagName!=="SELECT"){
  opts($("s-model"),inst,m.model);opts($("s-code"),inst,m.code_model,"(same as digest)");opts($("s-fb"),inst,m.fallback_models[0]||"","(none)")}
 const j=s.job,lg=$("log");if(j.name){lg.hidden=false;lg.textContent=j.log.join("\n");lg.scrollTop=lg.scrollHeight}
 $("b-tune").disabled=$("b-doc").disabled=$("b-pull").disabled=j.running;
 if(!document.querySelector("#cat input:checked:not(:disabled)")) renderCat(s.catalog)}
function renderCat(list){$("cat").innerHTML=`<table class="cat">`+list.map(m=>{
 const tags=[m.good_at==="both"?"digest + code":m.good_at, m.moe?"MoE (fast)":"", m.thai?"":"weak Thai"].filter(Boolean)
  .map(t=>`<span class="tag">${esc(t)}</span>`).join("")
  +(Object.keys(m.tested).length?Object.entries(m.tested).map(([k,v])=>`<span class="tag t">tested ${esc(k)}: ${esc(v)}</span>`).join(""):`<span class="tag">untested</span>`);
 return `<tr><td><input type="checkbox" value="${esc(m.name)}" ${m.installed?"disabled checked":""} aria-label="${esc(m.name)}"></td>
  <td><div class="nm">${m.recommended?"★ ":""}${esc(m.name)}</div><div class="sub">${esc(m.note)}</div>${tags}</td>
  <td style="text-align:right;white-space:nowrap">${m.size.toFixed(1)} GB<div class="sub">${m.installed?"installed":""}</div></td></tr>`}).join("")+`</table>`}
function tickRec(){S.catalog.forEach(m=>{const b=document.querySelector(`#cat input[value="${CSS.escape(m.name)}"]`);if(b&&!b.disabled)b.checked=m.recommended})}
async function pullSel(){const names=[...document.querySelectorAll("#cat input:checked:not(:disabled)")].map(b=>b.value);
 if(!names.length)return;await post("/api/pull",{names});refresh()}
async function refresh(){try{render(await (await fetch("/api/status")).json())}catch(e){}}
async function setMode(m){render(await post("/api/mode",{mode:m}))}
async function saveModels(){const fb=$("s-fb").value;render(await post("/api/models",{model:$("s-model").value,code_model:$("s-code").value,fallback_models:fb?[fb]:[]}))}
async function agent(name,action){const r=await post("/api/agent",{name,action});if(r.error)alert(r.error);else render(r)}
async function saveCouncil(){const members=[...document.querySelectorAll("#council input:checked")].map(b=>b.value);
 const r=await post("/api/council",{members});if(r.error)alert(r.error);else{councilDirty=false;render(r)}}
async function saveHandoff(){const r=await post("/api/handoff",{agent:$("h-agent").value,rounds:Number($("h-rounds").value)});
 if(r.error)alert(r.error);else render(r)}
async function job(name){await post("/api/"+name);refresh()}
$("council").addEventListener("change",()=>councilDirty=true);
render(__INIT__);setInterval(refresh,2500);
</script></body></html>
"""
