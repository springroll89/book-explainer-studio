"""Human-only approval records and hash validation."""
from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from .common import load_yaml, sha256_file, write_yaml

GATES = ("G1","G2","G3","G4","AV1","RELEASE","SOURCE","OVERRIDE")

def _approval_dir(project: Path) -> Path:
    d=Path(project)/"approvals"; d.mkdir(parents=True, exist_ok=True); return d

def _digest_paths(project: Path, gate: str, ep: int|None=None) -> tuple[list[str],str]:
    p=Path(project); paths=[]
    if gate=="G1": paths=[p/"analysis/book_brief.md",p/"analysis/coverage_review.yaml",p/"analysis/characters.yaml",p/"analysis/threads.yaml"]
    elif gate=="G2": paths=[p/"plan/episodes.yaml"]
    elif gate in ("G3","G4") and ep:
        ed=p/"episodes"/f"ep{ep:02d}"
        drafts=sorted(ed.glob("draft_v*.md"))
        paths=[(drafts[-1] if drafts else ed/"draft_v1.md"), ed/"review"] if gate=="G3" else [ed/"final.md", ed/"review"]
    elif gate=="AV1" and ep: paths=[p/"episodes"/f"ep{ep:02d}"/"production",p/"episodes"/f"ep{ep:02d}"/"preview"]
    elif gate=="RELEASE": paths=[p/"release/compliance.yaml"]
    existing=[]
    for x in paths:
        if x.is_file(): existing.append(x)
        elif x.is_dir():
            files = (y for y in x.rglob("*") if y.is_file() and "__pycache__" not in y.parts)
            if gate == "AV1":
                # Finder metadata is incidental and can change without any
                # production asset changing, so it must not stale AV1.
                files = (y for y in files if y.name != ".DS_Store")
            if gate == "G3" and x.name == "review":
                # G3 is bound to style/source review artifacts. Later G4 packets
                # are administrative sign-off materials and must not stale G3.
                files = (y for y in files if not y.relative_to(x).parts[0].startswith("g4_materials_"))
            existing.extend(sorted(files))
    records=[]
    for x in existing:
        records.append((str(x.resolve().relative_to(p.resolve())),sha256_file(x)))
    payload=json.dumps(records,ensure_ascii=False,separators=(",",":"))
    return [x for x,_ in records], hashlib.sha256(payload.encode()).hexdigest()

def list_valid(project: Path) -> list[dict]:
    out=[]
    for path in sorted(_approval_dir(Path(project)).glob("*.yaml")):
        try: d=load_yaml(path,{})
        except Exception: continue
        if not isinstance(d,dict) or d.get("revoked"): continue
        gate=d.get("gate")
        if gate not in GATES: continue
        if gate=="OVERRIDE":
            out.append({**d,"path":str(path),"valid":True}); continue
        files,digest=_digest_paths(Path(project),gate,d.get("ep"))
        out.append({**d,"path":str(path),"valid": digest==d.get("object_sha256") and files==d.get("object_files",[])})
    return out

def gate_state(project: Path, gate: str, ep: int|None=None) -> str:
    matches=[a for a in list_valid(project) if a.get("gate")==gate and a.get("ep")==ep]
    if any(a.get("valid") for a in matches): return "passed"
    if any(not a.get("valid") for a in matches): return "invalidated"
    revoked=False
    for path in _approval_dir(Path(project)).glob("*.yaml"):
        d=load_yaml(path,{})
        if isinstance(d,dict) and d.get("gate")==gate and d.get("ep")==ep and d.get("revoked"): revoked=True
    return "revoked" if revoked else "pending"

def approve(project: Path, gate: str, ep: int|None=None) -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["approve 只能由用户在交互式终端执行，代理或管道输入已拒绝"]}
    gate=gate.upper()
    if gate not in GATES: return {"passed":False,"errors":[f"未知闸门：{gate}"]}
    files,digest=_digest_paths(Path(project),gate,ep)
    print(f"待批准：{gate}{f' 第{ep}集' if ep else ''}\n对象哈希：{digest}\n文件：{len(files)} 个")
    expected=f"确认 {gate}"+(f" 第{ep}集" if ep else "")
    answer=input(f"请输入“{expected}”：").strip()
    if answer!=expected: return {"passed":False,"errors":["确认短语不匹配，未生成批准记录"]}
    user=load_yaml(Path.home()/".config/bookflow/user.yaml",{}) or {}
    approver=user.get("name") or user.get("user") or "本机用户"
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"{gate}"+(f"-ep{ep:02d}" if ep else "")+f"-{stamp}.yaml")
    write_yaml(path,{"gate":gate,"ep":ep,"object_files":files,"object_sha256":digest,"approved_at":datetime.now(timezone.utc).isoformat(),"approver":approver,"note":"用户在交互终端确认"})
    return {"passed":True,"gate":gate,"ep":ep,"path":str(path),"object_sha256":digest}

def revoke(project: Path, gate: str, ep: int|None=None, reason: str="") -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["revoke 只能由用户在交互式终端执行"]}
    answer=input(f"请输入“撤回 {gate.upper()}”以确认：").strip()
    if answer!=f"撤回 {gate.upper()}": return {"passed":False,"errors":["确认短语不匹配"]}
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"revoked-{gate.upper()}"+(f"-ep{ep:02d}" if ep else "")+f"-{stamp}.yaml")
    write_yaml(path,{"gate":gate.upper(),"ep":ep,"revoked":True,"revoked_at":datetime.now(timezone.utc).isoformat(),"reason":reason})
    return {"passed":True,"path":str(path)}

def override(project: Path, rule: str, reason: str) -> dict:
    if not sys.stdin.isatty(): return {"passed":False,"errors":["override 只能由用户在交互式终端执行"]}
    expected=f"确认授权 {rule}"
    if input(f"请输入“{expected}”：").strip()!=expected:
        return {"passed":False,"errors":["确认短语不匹配"]}
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path=_approval_dir(Path(project))/(f"OVERRIDE-{stamp}.yaml")
    write_yaml(path,{"gate":"OVERRIDE","rule":rule,"reason":reason,"approved_at":datetime.now(timezone.utc).isoformat(),"approver":"本机用户"})
    return {"passed":True,"path":str(path),"status":"preview_override"}
