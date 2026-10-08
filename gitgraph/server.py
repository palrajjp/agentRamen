"""Local-only, versioned HTTP API for GitGraph."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    GitGraphError,
    architecture,
    architecture_at,
    context_for,
    dependency_impact,
    explain_file,
    file_history,
    graph_at,
    graph_export,
    hotspots,
    repo_search,
    repository_status,
)

WEB_UI = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f3f5f0">
<title>GitGraph | Repository workspace</title>
<style>
:root{color-scheme:light;--paper:#f3f5f0;--surface:#fff;--ink:#202a25;--muted:#6a756d;--line:#dce2dc;--green:#21634a;--green-soft:#e5f0e8;--rust:#a74d37;--gold:#d3a33d;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:"Avenir Next","Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--sans);-webkit-font-smoothing:antialiased}button,input,textarea{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #d3a33d;outline-offset:2px}a{color:inherit}.topbar{height:64px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 30px}.brand{display:flex;align-items:center;gap:10px;text-decoration:none;font-size:18px;font-weight:700}.brand-glyph{display:grid;place-items:center;width:29px;height:29px;background:var(--green);color:#fff;border-radius:5px;font:700 16px var(--mono)}.top-meta{display:flex;align-items:center;gap:15px}.local-badge,.status-pill{font:700 10px var(--mono);letter-spacing:.08em}.local-badge{padding:5px 8px;background:#edf0eb;border-radius:3px;color:#58645b}.status-pill{color:var(--muted)}.status-pill:before{content:"";display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:var(--gold)}.status-pill[data-state="ready"]:before{background:var(--green)}.app-shell{display:grid;grid-template-columns:224px minmax(0,1fr);max-width:1520px;min-height:calc(100vh - 64px);margin:auto}.sidebar{display:flex;flex-direction:column;padding:27px 16px 20px 24px;border-right:1px solid var(--line)}.repo-label{padding:0 10px 24px;border-bottom:1px solid var(--line)}.eyebrow{display:block;color:var(--muted);font:700 10px var(--mono);letter-spacing:.1em;text-transform:uppercase}.repo-label strong{display:block;margin-top:8px;font-size:14px;overflow-wrap:anywhere}.repo-detail{display:block;margin-top:3px;color:var(--muted);font-size:12px}.nav-list{display:grid;gap:5px;margin-top:19px}.nav-item{display:flex;align-items:center;gap:11px;width:100%;padding:10px;border:0;border-radius:4px;background:transparent;color:#5e6961;text-align:left}.nav-item:hover{background:#e8ece6;color:var(--ink)}.nav-item[aria-current="page"]{background:var(--green-soft);color:var(--green);font-weight:700}.nav-mark{width:19px;color:inherit;font:12px var(--mono);text-align:center}.privacy-note{margin:auto 8px 0;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.privacy-dot{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:var(--green)}main{min-width:0;padding:40px clamp(24px,5vw,70px) 70px}.view{max-width:1040px;margin:0 auto}.view[hidden]{display:none}.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:29px}.page-head h1{margin:5px 0 0;font-size:30px;line-height:1.15;font-weight:650}.page-head p{margin:8px 0 0;color:var(--muted)}.primary,.secondary{min-height:40px;padding:0 15px;border:1px solid var(--green);border-radius:4px;background:var(--green);color:#fff;font-weight:650}.primary:hover{background:#194e3a}.secondary{background:var(--surface);color:var(--green)}.secondary:hover{background:var(--green-soft)}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:38px}.stat{min-width:0;padding:18px 16px 19px 0}.stat+.stat{padding-left:20px;border-left:1px solid var(--line)}.stat-label{display:block;color:var(--muted);font-size:12px}.stat-value{display:block;margin-top:7px;font:600 25px/1.15 var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.section-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}.section-head h2,.subhead{margin:0;font-size:17px;font-weight:650}.subhead{margin:25px 0 12px}.text-button{padding:5px 0;border:0;background:transparent;color:var(--green);font-weight:650}.text-button:hover{text-decoration:underline}.notice{margin:0 0 25px;padding:14px 16px;border-left:3px solid var(--gold);background:#fff9e9}.notice[hidden]{display:none}.notice code{font:12px var(--mono)}.list{border-top:1px solid var(--line)}.list-item{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:13px 2px;border-bottom:1px solid var(--line)}.list-item strong,.file-path{display:block;overflow-wrap:anywhere;font:600 13px var(--mono)}.list-item small,.file-meta{display:block;margin-top:4px;color:var(--muted);font-size:12px}.list-count{flex:none;color:var(--rust);font:600 13px var(--mono)}.form-panel{padding:19px;background:var(--surface);border:1px solid var(--line);border-radius:5px}.field-label{display:block;margin-bottom:7px;font-size:13px;font-weight:650}.task-input,.search-input,.number-input{width:100%;padding:11px 12px;border:1px solid #c9d2ca;border-radius:4px;background:#fff;color:var(--ink)}.task-input{min-height:120px;resize:vertical}.form-row{display:flex;align-items:flex-end;gap:15px;margin-top:13px}.budget-field{width:180px}.budget-field .field-label{margin-bottom:5px}.number-input{height:40px;font-family:var(--mono)}.form-row .primary{margin-left:auto}.results-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:24px 0 10px}.results-head h2{margin:0;font-size:16px}.result-meta{color:var(--muted);font-size:12px}.file-list{display:grid;gap:10px}.file-item{padding:15px 16px;background:var(--surface);border:1px solid var(--line);border-radius:5px}.file-top{display:flex;align-items:flex-start;justify-content:space-between;gap:15px}.file-path{font-size:13px}.file-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.badge{padding:3px 7px;border-radius:3px;background:#edf0eb;color:#59655d;font:10px var(--mono)}.badge.score{background:var(--green-soft);color:var(--green)}.file-item pre{margin:13px 0 0;padding:12px;overflow:auto;border-left:2px solid var(--gold);background:#f6f7f4;color:#354139;font:12px/1.55 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}.symbol-line{margin-top:8px;color:var(--muted);font-size:12px}.inspect{margin-top:10px;padding:0;border:0;background:transparent;color:var(--green);font-size:12px;font-weight:650}.inspect:hover{text-decoration:underline}.file-details{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);color:#4c5951;font-size:12px}.file-details[hidden]{display:none}.empty-state,.error-state{padding:23px 16px;border:1px dashed #c9d2ca;border-radius:4px;color:var(--muted);text-align:center}.error-state{border-color:#d9a194;background:#fff7f5;color:#8b3927}.architecture-grid{display:grid;grid-template-columns:1fr 1fr;gap:34px}.breakdown{border-top:1px solid var(--line)}.breakdown-row{padding:11px 0;border-bottom:1px solid var(--line)}.breakdown-label{display:flex;justify-content:space-between;gap:12px;font-size:13px}.breakdown-label strong{font:600 12px var(--mono)}.bar-track{height:5px;margin-top:8px;background:#e4e9e3}.bar-fill{height:100%;background:var(--green)}.relationship-summary{display:flex;flex-wrap:wrap;gap:8px;margin-top:27px}.relationship-chip{padding:7px 10px;border:1px solid var(--line);background:#fff;border-radius:3px;color:#47534b;font:11px var(--mono)}.loading{color:var(--muted);font-size:13px}.inline-error{color:var(--rust);font-size:13px}
@media(max-width:850px){.app-shell{grid-template-columns:1fr}.sidebar{padding:12px 16px;border-right:0;border-bottom:1px solid var(--line)}.repo-label,.privacy-note{display:none}.nav-list{display:flex;overflow:auto;margin:0;gap:4px}.nav-item{width:auto;white-space:nowrap}.nav-mark{display:none}main{padding:30px 22px 50px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat:nth-child(3){padding-left:0;border-left:0;border-top:1px solid var(--line)}.stat:nth-child(4){border-top:1px solid var(--line)}}@media(max-width:540px){.topbar{height:56px;padding:0 16px}.app-shell{min-height:calc(100vh - 56px)}.local-badge{display:none}main{padding:25px 15px 38px}.page-head{align-items:flex-start;flex-direction:column;margin-bottom:22px}.page-head h1{font-size:25px}.stats{margin-bottom:28px}.stat{padding:14px 8px 14px 0}.stat+.stat{padding-left:12px}.stat-value{font-size:20px}.form-panel{padding:14px}.form-row{align-items:stretch;flex-wrap:wrap}.budget-field{width:calc(50% - 8px)}.form-row .primary{width:100%;margin-left:0}.architecture-grid{grid-template-columns:1fr;gap:22px}.file-top{flex-direction:column}.file-badges{justify-content:flex-start}.list-item{align-items:flex-start}}
</style>
<style>
@media(max-width:850px){.app-shell{grid-template-columns:minmax(0,1fr)}.sidebar,.nav-list,main{min-width:0;max-width:100%}.nav-list{width:100%}}
</style>
<body>
<header class="topbar">
    <a class="brand" href="#overview" aria-label="GitGraph overview"><span class="brand-glyph">G</span><span>GitGraph</span></a>
    <div class="top-meta"><span class="local-badge">LOCAL WORKSPACE</span><span class="status-pill" id="connection-status" data-state="loading">Connecting</span></div>
</header>
<div class="app-shell">
    <aside class="sidebar" aria-label="Workspace navigation">
        <div class="repo-label"><span class="eyebrow">Workspace</span><strong>Current repository</strong><span class="repo-detail" id="repo-detail">Checking index</span></div>
        <nav class="nav-list" aria-label="Views">
            <button class="nav-item" data-view="overview" aria-current="page"><span class="nav-mark">01</span>Overview</button>
            <button class="nav-item" data-view="context"><span class="nav-mark">02</span>Task context</button>
            <button class="nav-item" data-view="search"><span class="nav-mark">03</span>Search</button>
            <button class="nav-item" data-view="architecture"><span class="nav-mark">04</span>Architecture</button>
        </nav>
        <div class="privacy-note"><span class="privacy-dot"></span>Index stays on this machine</div>
    </aside>
    <main id="main-content">
        <section class="view" id="view-overview" aria-labelledby="overview-title">
            <div class="page-head"><div><span class="eyebrow">Repository intelligence</span><h1 id="overview-title">Repository map</h1><p>Code structure and change history, in one place.</p></div><button class="primary" data-open-view="context">Build task context</button></div>
            <div class="notice" id="index-notice" hidden>No indexed files yet. In this repository, run <code>gitgraph init</code> in a terminal.</div>
            <div class="stats" aria-live="polite">
                <div class="stat"><span class="stat-label">Indexed files</span><strong class="stat-value" id="stat-files">--</strong></div>
                <div class="stat"><span class="stat-label">History commits</span><strong class="stat-value" id="stat-commits">--</strong></div>
                <div class="stat"><span class="stat-label">Languages</span><strong class="stat-value" id="stat-languages">--</strong></div>
                <div class="stat"><span class="stat-label">Latest commit</span><strong class="stat-value" id="stat-latest">--</strong></div>
            </div>
            <div class="section-head"><h2>Frequently changed files</h2><button class="text-button" data-open-view="architecture">Explore architecture</button></div>
            <div class="list" id="hotspot-list" aria-live="polite"><div class="loading">Loading repository activity</div></div>
        </section>
        <section class="view" id="view-context" aria-labelledby="context-title" hidden>
            <div class="page-head"><div><span class="eyebrow">Context builder</span><h1 id="context-title">What are you working on?</h1><p>Get a ranked, budget-aware bundle from this repository.</p></div></div>
            <form class="form-panel" id="context-form">
                <label class="field-label" for="task">Task</label><textarea class="task-input" id="task" placeholder="For example: trace how session expiry is handled" required></textarea>
                <div class="form-row"><div class="budget-field"><label class="field-label" for="budget">Token budget</label><input class="number-input" id="budget" type="number" min="100" step="100" value="2000"></div><button class="primary" type="submit">Build context</button></div>
            </form>
            <div class="results-head"><h2>Selected files</h2><span class="result-meta" id="context-summary"></span></div>
            <div class="file-list" id="context-results" aria-live="polite"><div class="empty-state">Describe a task to build context.</div></div>
        </section>
        <section class="view" id="view-search" aria-labelledby="search-title" hidden>
            <div class="page-head"><div><span class="eyebrow">Indexed source</span><h1 id="search-title">Find a file</h1><p>Search paths, symbols, and imports.</p></div></div>
            <form class="form-panel" id="search-form"><label class="field-label" for="query">Search</label><div class="form-row"><input class="search-input" id="query" type="search" placeholder="AuthService, billing, login..." required><button class="primary" type="submit">Search files</button></div></form>
            <div class="results-head"><h2>Matches</h2><span class="result-meta" id="search-summary"></span></div>
            <div class="file-list" id="search-results" aria-live="polite"><div class="empty-state">Search for a path, symbol, or import.</div></div>
        </section>
        <section class="view" id="view-architecture" aria-labelledby="architecture-title" hidden>
            <div class="page-head"><div><span class="eyebrow">Indexed structure</span><h1 id="architecture-title">Architecture</h1><p>Files grouped by language and top-level module.</p></div><button class="secondary" id="refresh-architecture" type="button">Refresh</button></div>
            <div class="architecture-grid"><div><h2 class="subhead">Languages</h2><div class="breakdown" id="language-breakdown"></div></div><div><h2 class="subhead">Modules</h2><div class="breakdown" id="module-breakdown"></div></div></div>
            <h2 class="subhead">Relationships</h2><div class="relationship-summary" id="relationship-summary"></div>
        </section>
    </main>
</div>
<script>
const byId=id=>document.getElementById(id);
const api=async(url,options)=>{const response=await fetch(url,options);const value=await response.json();if(!response.ok)throw Error(value.error||response.statusText);return value};
const node=(tag,className,text)=>{const item=document.createElement(tag);if(className)item.className=className;if(text!==undefined)item.textContent=text;return item};
function setMessage(target,message,isError=false){target.replaceChildren(node('div',isError?'error-state':'empty-state',message))}
function showView(name){document.querySelectorAll('.view').forEach(view=>{view.hidden=view.id!=='view-'+name});document.querySelectorAll('.nav-item').forEach(button=>{if(button.dataset.view===name)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')});if(name==='architecture')loadArchitecture();if(name==='overview')loadHotspots()}
document.querySelectorAll('[data-view],[data-open-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view||button.dataset.openView)));
function renderFile(target,file,withExcerpt){
    const article=node('article','file-item');const top=node('div','file-top');const heading=node('div');heading.append(node('div','file-path',file.path));
    const meta=[file.language,file.symbols&&file.symbols.length?file.symbols.slice(0,5).join(', '):null].filter(Boolean).join(' / ');if(meta)heading.append(node('div','file-meta',meta));top.append(heading);
    const badges=node('div','file-badges');if(file.score!==undefined)badges.append(node('span','badge score','match '+Number(file.score).toFixed(3)));if(file.estimated_tokens!==undefined)badges.append(node('span','badge',file.estimated_tokens+' tokens'));if(file.recent_change)badges.append(node('span','badge',file.recent_change));if(badges.childElementCount)top.append(badges);article.append(top);
    if(withExcerpt&&file.excerpt)article.append(node('pre','',file.excerpt));if(file.relationships&&file.relationships.length){const relationText=file.relationships.map(item=>item.path+' ('+item.relationship+')').join(' · ');article.append(node('div','symbol-line','Related: '+relationText))}
    const inspect=node('button','inspect','File relationships');inspect.type='button';const details=node('div','file-details','');details.hidden=true;let loaded=false;
    inspect.addEventListener('click',async()=>{details.hidden=!details.hidden;if(details.hidden||loaded)return;details.textContent='Loading file details';try{const info=await api('/api/v1/files/'+encodeURIComponent(file.path));const lines=[];for(const key of ['dependencies','direct_dependents','indirect_dependents','symbols','imports']){const value=info[key];if(Array.isArray(value)&&value.length)lines.push(key.replaceAll('_',' ')+': '+value.join(', '))}details.textContent=lines.length?lines.join('\n'):'No indexed relationships';loaded=true}catch(error){details.textContent=error.message}});
    article.append(inspect,details);target.append(article)
}
async function loadStatus(){
    const pill=byId('connection-status');try{const status=await api('/api/v1/repository');byId('stat-files').textContent=status.files.toLocaleString();byId('stat-commits').textContent=status.commits.toLocaleString();byId('stat-languages').textContent=Object.keys(status.languages).length.toString();byId('stat-latest').textContent=status.last_commit?status.last_commit.slice(0,7):'--';byId('repo-detail').textContent=status.files?status.files.toLocaleString()+' files indexed':'Index not built';byId('index-notice').hidden=status.files>0;pill.textContent=status.files?'Indexed':'Not indexed';pill.dataset.state=status.files?'ready':'empty';if(!status.files)byId('hotspot-list').replaceChildren(node('div','empty-state','No indexed activity yet. Run gitgraph init in this repository.'))}catch(error){pill.textContent='Unavailable';pill.dataset.state='error';byId('repo-detail').textContent='API unavailable';byId('index-notice').hidden=false;byId('index-notice').textContent=error.message}}
async function loadHotspots(){const target=byId('hotspot-list');try{const rows=await api('/api/v1/hotspots');if(!rows.length){setMessage(target,'No change history indexed yet.');return}target.replaceChildren();rows.slice(0,8).forEach(row=>{const item=node('div','list-item');const info=node('div');info.append(node('strong','',row.path));info.append(node('small','',row.authors+' contributors · last changed '+(row.last_changed||'unknown')));item.append(info,node('span','list-count',row.changes+' changes'));target.append(item)})}catch(error){setMessage(target,error.message,true)}}
function showFiles(target,files,withExcerpt){if(!files.length){setMessage(target,'No matching files found.');return}target.replaceChildren();files.forEach(file=>renderFile(target,file,withExcerpt))}
byId('context-form').addEventListener('submit',async event=>{event.preventDefault();const target=byId('context-results');const button=event.currentTarget.querySelector('button[type="submit"]');const previous=button.textContent;button.disabled=true;button.textContent='Building…';setMessage(target,'Ranking repository context');byId('context-summary').textContent='';try{const result=await api('/api/v1/context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:byId('task').value,token_budget:Number(byId('budget').value)})});showFiles(target,result.files,true);byId('context-summary').textContent=result.files.length+' files · '+result.estimated_tokens+'/'+result.token_budget+' '+result.token_count_method+' tokens · '+result.confidence+' confidence'}catch(error){setMessage(target,error.message,true)}finally{button.disabled=false;button.textContent=previous}});
byId('search-form').addEventListener('submit',async event=>{event.preventDefault();const target=byId('search-results');const button=event.currentTarget.querySelector('button[type="submit"]');button.disabled=true;byId('search-summary').textContent='Searching';try{const results=await api('/api/v1/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:byId('query').value,limit:30})});showFiles(target,results,false);byId('search-summary').textContent=results.length+' matches'}catch(error){setMessage(target,error.message,true);byId('search-summary').textContent=''}finally{button.disabled=false}});
function renderBreakdown(target,values){const rows=Object.entries(values||{}).sort((left,right)=>right[1]-left[1]);if(!rows.length){setMessage(target,'No indexed data.');return}const maximum=Math.max(...rows.map(row=>row[1]));target.replaceChildren();rows.forEach(([label,count])=>{const row=node('div','breakdown-row');const head=node('div','breakdown-label');head.append(node('span','',label),node('strong','',count.toLocaleString()));const track=node('div','bar-track');const fill=node('div','bar-fill');fill.style.width=Math.max(2,count/maximum*100)+'%';track.append(fill);row.append(head,track);target.append(row)})}
async function loadArchitecture(){const languages=byId('language-breakdown');if(languages.dataset.loaded==='true')return;languages.dataset.loaded='loading';try{const data=await api('/api/v1/architecture');renderBreakdown(languages,data.languages);renderBreakdown(byId('module-breakdown'),data.modules);const target=byId('relationship-summary');const rows=Object.entries(data.relationships||{});target.replaceChildren();if(!rows.length)target.append(node('span','relationship-chip','No relationships indexed'));rows.forEach(([name,count])=>target.append(node('span','relationship-chip',name.replaceAll('_',' ').toLowerCase()+' · '+count)));languages.dataset.loaded='true'}catch(error){languages.dataset.loaded='';setMessage(languages,error.message,true)}}
byId('refresh-architecture').addEventListener('click',()=>{byId('language-breakdown').dataset.loaded='';loadArchitecture()});
loadStatus();loadHotspots();
</script>
</body>
</html>"""


def create_server(root: Path, host: str = "127.0.0.1", port: int = 8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GitGraph/0.1"

        def _respond(self, status: int, value: object) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _query(self):
            parsed = urlparse(self.path)
            return parsed.path, parse_qs(parsed.query)

        def do_GET(self) -> None:
            path, query = self._query()
            try:
                if path in ("/", "/ui"):
                    payload = WEB_UI.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'",
                    )
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                elif path == "/health":
                    value = {"status": "ok"}
                elif path == "/api/v1/repository":
                    value = repository_status(root)
                elif path == "/api/v1/architecture":
                    revision = query.get("at", [""])[0]
                    value = architecture_at(root, revision) if revision else architecture(root)
                elif path == "/api/v1/graph":
                    revision = query.get("at", [""])[0]
                    value = graph_at(root, revision) if revision else graph_export(root)
                elif path.startswith("/api/v1/files/"):
                    file_path = unquote(path.removeprefix("/api/v1/files/"))
                    value = explain_file(root, file_path)
                elif path == "/api/v1/impact":
                    value = dependency_impact(root, query.get("path", [""])[0])
                elif path == "/api/v1/history":
                    value = file_history(root, query.get("path", [""])[0])
                elif path == "/api/v1/hotspots":
                    value = hotspots(root)
                else:
                    self._respond(404, {"error": "Not found"})
                    return
                self._respond(200, value)
            except GitGraphError as exc:
                self._respond(404, {"error": str(exc)})
            except (OSError, ValueError) as exc:
                self._respond(400, {"error": str(exc)})

        def do_POST(self) -> None:
            path, _ = self._query()
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 0 or content_length > 1_000_000:
                    self._respond(413, {"error": "Request body exceeds 1 MB"})
                    return
                request = json.loads(self.rfile.read(content_length) or b"{}")
                if not isinstance(request, dict):
                    raise ValueError("Request body must be a JSON object")
                if path == "/api/v1/context":
                    value = context_for(
                        root,
                        str(request.get("task", "")),
                        int(request["token_budget"])
                        if request.get("token_budget") is not None
                        else None,
                    )
                elif path == "/api/v1/search":
                    value = repo_search(
                        root, str(request.get("query", "")), int(request.get("limit", 20))
                    )
                else:
                    self._respond(404, {"error": "Not found"})
                    return
                self._respond(200, value)
            except (GitGraphError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._respond(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(root, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
