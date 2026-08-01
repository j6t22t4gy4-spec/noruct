"""One operator-confirmed DingTalk static robot webhook delivery."""
from __future__ import annotations
import json, os, re, tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output

_HEADER=re.compile(r"(?m)^\[dingtalk_channel\][ \t]*(?:\r?\n|$)"); _TABLE=re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)"); _ENV=re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
def _webhook(value: str) -> str:
    raw=value.strip(); p=urlsplit(raw)
    if p.scheme!="https" or p.hostname not in {"oapi.dingtalk.com","oapi.dingtalk.cn"} or not p.path.startswith("/robot/send") or not p.query: raise ValueError("DingTalk webhook must be an HTTPS official robot send URL with its configured query")
    return raw
@dataclass(frozen=True,slots=True)
class DingTalkChannelConfig:
    webhook_env:str="DINGTALK_WEBHOOK_URL"; max_message_bytes:int=4_000; timeout_seconds:float=15.0
    def validate(self)->None:
        if not _ENV.fullmatch(self.webhook_env): raise ValueError("DingTalk webhook environment variable name is invalid")
        if not 1<=self.max_message_bytes<=20_000: raise ValueError("DingTalk message limit must be between 1 and 20000 bytes")
        if not 1<=self.timeout_seconds<=45: raise ValueError("DingTalk timeout must be between 1 and 45 seconds")
@dataclass(frozen=True,slots=True)
class DingTalkDeliveryResult:
    delivered:bool; message_bytes:int; automatic_delivery:bool; output:str
    def to_dict(self)->Mapping[str,object]: return asdict(self)
def _without(text:str)->str:
    match=_HEADER.search(text)
    if match is None:return text.strip()
    following=_TABLE.search(text,match.end()); return (text[:match.start()]+(text[following.start():] if following else "")).strip()
def _write(path:Path,text:str)->Path:
    target=path.expanduser().resolve();target.parent.mkdir(parents=True,exist_ok=True);d,t=tempfile.mkstemp(prefix=".noruct-config-",dir=target.parent)
    try:
        with os.fdopen(d,"w",encoding="utf-8",newline="\n") as h:h.write(text);h.flush();os.fsync(h.fileno())
        os.chmod(t,0o600);os.replace(t,target)
    finally:
        try:os.unlink(t)
        except FileNotFoundError:pass
    return target
def write_dingtalk_channel_settings(path:Path,config:DingTalkChannelConfig)->Path:
    config.validate();target=path.expanduser().resolve();existing=target.read_text(encoding="utf-8") if target.is_file() else "";q=json.dumps
    table="\n".join(("[dingtalk_channel]","enabled = true",f"webhook_env = {q(config.webhook_env)}",f"max_message_bytes = {config.max_message_bytes}",f"timeout_seconds = {config.timeout_seconds:g}",""));remain=_without(existing);return _write(target,(remain+"\n\n" if remain else "")+table)
def remove_dingtalk_channel_settings(path:Path)->bool:
    target=path.expanduser().resolve()
    if not target.is_file():return False
    text=target.read_text(encoding="utf-8")
    if _HEADER.search(text) is None:return False
    remain=_without(text);_write(target,remain+("\n" if remain else ""));return True
def dingtalk_channel_config_from_settings(settings:Mapping[str,Any])->DingTalkChannelConfig|None:
    raw=settings.get("dingtalk_channel")
    if not isinstance(raw,Mapping) or raw.get("enabled") is not True:return None
    env,maximum,timeout=raw.get("webhook_env","DINGTALK_WEBHOOK_URL"),raw.get("max_message_bytes",4_000),raw.get("timeout_seconds",15.0)
    if not isinstance(env,str) or not isinstance(maximum,int) or isinstance(maximum,bool) or not isinstance(timeout,(int,float)):raise ValueError("DingTalk channel configuration is malformed")
    config=DingTalkChannelConfig(env.strip(),maximum,float(timeout));config.validate();return config
def dingtalk_channel_status(config:DingTalkChannelConfig|None)->Mapping[str,object]:
    if config is None:return {"enabled":False,"authority":"no_dingtalk_channel","next_action":"noruct channel dingtalk-configure"}
    return {"enabled":True,"webhook_environment":config.webhook_env,"ready":bool(os.environ.get(config.webhook_env)),"automatic_delivery":False,"authority":"operator_confirmed_single_dingtalk_robot_webhook_not_an_employee_tool","next_action":None if os.environ.get(config.webhook_env) else f"Set {config.webhook_env} in the operator shell."}
def deliver_dingtalk_message(config:DingTalkChannelConfig,*,title:str,message:str)->DingTalkDeliveryResult:
    config.validate(); body=str(message or "").strip();heading=str(title or "").strip()
    if not body or not heading or "\x00" in body or "\r" in heading or "\n" in heading:raise ValueError("DingTalk title and message must be non-empty safe text")
    size=len(body.encode("utf-8"))
    if size>config.max_message_bytes:raise ValueError("DingTalk message exceeds the configured byte limit")
    endpoint=os.environ.get(config.webhook_env)
    if not endpoint:raise ValueError(f"DingTalk webhook environment variable is not set: {config.webhook_env}")
    request=Request(_webhook(endpoint),data=json.dumps({"msgtype":"markdown","markdown":{"title":heading,"text":body}},ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json; charset=utf-8","Accept":"application/json"},method="POST")
    try:
        with urlopen(request,timeout=config.timeout_seconds) as response:response.read(32768)
    except HTTPError as exc:return DingTalkDeliveryResult(False,size,False,redact_terminal_output(f"DingTalk HTTP {exc.code}",force=True))
    except URLError as exc:return DingTalkDeliveryResult(False,size,False,redact_terminal_output(f"DingTalk connection failed: {exc.reason}",force=True))
    return DingTalkDeliveryResult(True,size,False,"accepted")
