"""Minimal ONVIF client: WS-Discovery on the local network and the SOAP calls needed
to add a camera (device information, media profiles, stream URI).

Only the Profile S subset NIRNAY needs is implemented, over plain HTTP(S) SOAP with a
WS-Security UsernameToken (password digest). No PTZ or imaging control is exposed.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger

log = get_logger("onvif")

MULTICAST = ("239.255.255.250", 3702)
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <s:Header><a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
  <a:MessageID>uuid:{mid}</a:MessageID><a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
  <a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>
 <s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>"""


class OnvifError(Exception):
    pass


def _scope_value(scopes: list[str], key: str) -> str | None:
    prefix = f"onvif://www.onvif.org/{key}/"
    for s in scopes:
        if s.startswith(prefix):
            return s[len(prefix):].replace("%20", " ").replace("_", " ")
    return None


def parse_probe_match(xml: bytes) -> list[dict[str, Any]]:
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for m in root.iter(f"{{{NS['d']}}}ProbeMatch"):
        xaddrs = (m.findtext("d:XAddrs", default="", namespaces=NS) or "").split()
        scopes = (m.findtext("d:Scopes", default="", namespaces=NS) or "").split()
        ep = m.findtext("a:EndpointReference/a:Address", default="", namespaces=NS)
        if not xaddrs:
            continue
        host = urlsplit(xaddrs[0]).hostname
        out.append({"endpoint": ep, "xaddrs": xaddrs, "host": host, "name": _scope_value(scopes, "name"),
                    "hardware": _scope_value(scopes, "hardware"), "location": _scope_value(scopes, "location"), "scopes": scopes[:20]})
    return out


def discover(timeout_s: float = 3.0, interface_ip: str | None = None) -> dict[str, Any]:
    """Send a WS-Discovery probe and collect ProbeMatches until the timeout."""
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if interface_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        sock.settimeout(0.3)
        msg = PROBE.format(mid=uuid.uuid4()).encode()
        for _ in range(2):  # UDP is lossy: probe twice
            try:
                sock.sendto(msg, MULTICAST)
            except OSError as e:
                errors.append(f"multicast send failed: {e}")
                break
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError as e:
                errors.append(str(e))
                break
            for dev in parse_probe_match(data):
                dev["responder"] = addr[0]
                found[dev["endpoint"] or dev["xaddrs"][0]] = dev
    finally:
        sock.close()
    return {"devices": list(found.values()), "timeout_s": timeout_s, "errors": errors,
            "note": "WS-Discovery only reaches cameras on the same L2 network segment as this server."}


def _security_header(username: str | None, password: str | None) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + (password or "").encode()).digest()).decode()
    return f"""<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{_esc(username)}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class OnvifClient:
    def __init__(self, xaddr: str, username: str | None = None, password: str | None = None, timeout_s: float = 5.0) -> None:
        self.xaddr = xaddr
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        self.media_xaddr: str | None = None

    def _call(self, url: str, body: str) -> ET.Element:
        env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" '
               f'xmlns:tt="{NS["tt"]}">{_security_header(self.username, self.password)}<s:Body>{body}</s:Body></s:Envelope>')
        try:
            r = httpx.post(url, content=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=self.timeout_s,
                           verify=False)  # noqa: S501 - cameras commonly use self-signed certificates on the local network
        except httpx.HTTPError as e:
            raise OnvifError(f"cannot reach {urlsplit(url).netloc}: {type(e).__name__}") from None
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise OnvifError(f"device returned HTTP {r.status_code} with a non-SOAP body") from None
        fault = root.find(".//s:Fault", NS)
        if fault is not None or r.status_code >= 400:
            reason = " ".join(t.strip() for t in fault.itertext() if t.strip()) if fault is not None else f"HTTP {r.status_code}"
            if (r.status_code in (400, 401) and "auth" in reason.lower()) or "NotAuthorized" in reason:
                raise OnvifError("authentication failed: check the ONVIF username/password")
            raise OnvifError(f"SOAP fault: {reason[:300]}")
        return root

    def device_information(self) -> dict[str, Any]:
        root = self._call(self.xaddr, "<tds:GetDeviceInformation/>")
        info = root.find(".//tds:GetDeviceInformationResponse", NS)
        if info is None:
            return {}
        return {k: info.findtext(f"tds:{k}", default=None, namespaces=NS) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

    def _media(self) -> str:
        if self.media_xaddr:
            return self.media_xaddr
        root = self._call(self.xaddr, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>')
        x = root.findtext(".//tt:Media/tt:XAddr", default=None, namespaces=NS)
        self.media_xaddr = x or self.xaddr
        return self.media_xaddr

    def profiles(self) -> list[dict[str, Any]]:
        root = self._call(self._media(), "<trt:GetProfiles/>")
        out = []
        for p in root.iter(f"{{{NS['trt']}}}Profiles"):
            enc = p.find("tt:VideoEncoderConfiguration", NS)
            res = enc.find("tt:Resolution", NS) if enc is not None else None
            rate = enc.find("tt:RateControl", NS) if enc is not None else None
            out.append({"token": p.get("token"), "name": p.findtext("tt:Name", default="", namespaces=NS),
                        "encoding": enc.findtext("tt:Encoding", default=None, namespaces=NS) if enc is not None else None,
                        "resolution": f"{res.findtext('tt:Width', namespaces=NS)}x{res.findtext('tt:Height', namespaces=NS)}" if res is not None else None,
                        "fps": float(rate.findtext("tt:FrameRateLimit", default="0", namespaces=NS) or 0) if rate is not None else None})
        return out

    def stream_uri(self, profile_token: str) -> str:
        body = (f"<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>"
                f"</tt:Transport></trt:StreamSetup><trt:ProfileToken>{_esc(profile_token)}</trt:ProfileToken></trt:GetStreamUri>")
        root = self._call(self._media(), body)
        uri = root.findtext(".//trt:MediaUri/tt:Uri", default=None, namespaces=NS)
        if not uri:
            raise OnvifError("device returned no stream URI")
        return uri

    def probe(self) -> dict[str, Any]:
        """Device info plus every profile with its RTSP URI (credentials never included)."""
        info = self.device_information()
        profiles = self.profiles()
        for p in profiles:
            try:
                p["rtsp_uri"] = self.stream_uri(p["token"])
            except OnvifError as e:
                p["rtsp_uri"], p["error"] = None, str(e)
        return {"xaddr": self.xaddr, "device": info, "profiles": profiles}
