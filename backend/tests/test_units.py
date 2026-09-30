"""Unit tests: OCR normalisation and temporal fusion, the identity-switch guard, privacy
masking, WebSocket event filtering, ONVIF parsing, rate limiting and the plate veto of identity
resolution."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.api.deps import CurrentUser
from app.api.routes.ws import filter_event
from app.auth.permissions import ROLE_PERMISSIONS
from app.camera.onvif import client as onvif
from app.core.bus import ui_event
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security import plate_pseudonym
from app.ml.ocr.fusion import PlateReadSample, fuse_reads
from app.ml.ocr.normalize import is_valid_plate, normalize_plate, plate_similarity, weighted_edit_distance
from app.ml.pipelines.anpr_pipeline import CameraPipeline, PipelineConfig, TrackState
from app.services import privacy
from app.services.identity import plate_veto
from app.services.settings_service import _defaults


# ------------------------------------------------------------------ plates
def test_normalisation_keeps_raw_and_fixes_confusions() -> None:
    n = normalize_plate("up 32-ab 1O34")
    assert n.raw == "up 32-ab 1O34"
    assert n.normalized == "UP32AB1034" and n.is_valid
    assert any(c.original == "O" and c.corrected == "0" for c in n.corrections)
    assert normalize_plate("22 BH 1234 AA").normalized == "22BH1234AA"
    assert not is_valid_plate("HELLO")


def test_weighted_distance_prefers_ocr_confusions() -> None:
    assert weighted_edit_distance("UP32AB1034", "UP32AB1O34") < weighted_edit_distance("UP32AB1034", "UP32AB1734")
    assert weighted_edit_distance("UP32AB1034", "UP32AB1034") == 0


def _read(text: str, conf: float = 0.95) -> PlateReadSample:
    n = normalize_plate(text)
    return PlateReadSample(text, n.normalized, conf, [conf] * len(n.normalized), n.is_valid, 0.0)


def test_fusion_votes_across_frames() -> None:
    f = fuse_reads([_read("UP32AB1234"), _read("UP32AB1284", 0.6), _read("UP32AB1234"), _read("UP32AB1234", 0.9)])
    assert f is not None and f.text == "UP32AB1234" and f.votes == 3
    single = fuse_reads([_read("UP32AB1234", 0.99)])
    assert single is not None and single.confidence < f.confidence  # one read cannot be corroborated


def test_fusion_ignores_plate_fragments() -> None:
    # a partly occluded plate read as "I" must never become a vehicle's plate
    assert fuse_reads([_read("I", 0.99), _read("I", 0.99), _read("I", 0.95)]) is None
    f = fuse_reads([_read("I", 0.99), _read("I", 0.99), _read("UP32AB1234", 0.8)])
    assert f is not None and f.text == "UP32AB1234"
    # a truncated read of an occluded plate ("UP32K") is a fragment too
    assert fuse_reads([_read("UP32K", 0.95), _read("UP32K", 0.93)]) is None
    assert fuse_reads([_read("DL1C12", 0.9), _read("DL1C12", 0.9)]).text == "DL1C12"


def test_identity_switch_splits_track() -> None:
    pipe = CameraPipeline.__new__(CameraPipeline)
    pipe.cfg = PipelineConfig()
    st = TrackState(1, 0.0, 1.0)
    st.reads = [_read("UP32HN4412", 0.99) for _ in range(4)]
    assert pipe._switch_check(st, _read("UP32HN4417", 0.97)) == "ok"  # a one-character misread is not a switch
    assert pipe._switch_check(st, _read("UP32HN4412", 0.99)) == "ok"
    assert pipe._switch_check(st, _read("22BH7176RC", 0.99)) == "hold"
    st.conflicts.append((_read("22BH7176RC", 0.99), [0, 0, 1, 1], np.zeros((4, 4, 3), np.uint8)))
    assert pipe._switch_check(st, _read("22BH7176RC", 0.98)) == "split"
    assert pipe._switch_check(TrackState(2, 0, 1), _read("22BH7176RC", 0.99)) == "ok"  # nothing corroborated yet


def test_split_track_emits_first_vehicle_and_keeps_second() -> None:
    pipe = CameraPipeline.__new__(CameraPipeline)
    pipe.cfg = PipelineConfig()
    pipe.camera_id = "T"
    pipe.states, pipe.ring = {}, []
    pipe.counters = {"vehicles": 0, "track_splits": 0}
    finalized = []
    pipe._finalize = lambda tid, ts, keep=False: finalized.append(pipe.states.pop(tid)) or {"type": "observation", "plate": "old"}  # type: ignore[method-assign]
    st = pipe.states[7] = TrackState(7, 0.0, 10.0)
    st.reads = [_read("UP32HN4412") for _ in range(4)]
    st.path = [(float(t), 100.0, 100.0 + t) for t in range(10)]
    st.motion_samples = [(float(t), 100.0, 100.0 + t, 50.0, "car") for t in range(10)]
    st.last_box = [0, 0, 8, 8]
    a, b = _read("22BH7176RC"), _read("22BH7176RC")
    a.ts, b.ts = 6.0, 7.0
    st.conflicts = [(a, [0, 0, 1, 1], np.zeros((2, 2, 3), np.uint8)), (b, [0, 0, 1, 1], np.zeros((2, 2, 3), np.uint8))]
    ev, new = pipe._split_track(st, 7.0, np.zeros((16, 16, 3), np.uint8))
    assert ev == {"type": "observation", "plate": "old"} and finalized[0] is st
    assert all(p[0] < 6.0 for p in st.path) and all(p[0] >= 6.0 for p in new.path)
    assert pipe.states[7] is new and [r.normalized for r in new.reads] == ["22BH7176RC", "22BH7176RC"]
    assert pipe.counters["track_splits"] == 1


# ------------------------------------------------------------------ privacy
def test_mask_replaces_plates_everywhere() -> None:
    obj = {"plate_text": "UP32AB1234", "title": "Wrong-way vehicle UP32AB1234 on NH-27", "nested": [{"plate_raw": "UP 32 AB 1234"}],
           "plate_reads_raw": [1], "count": 3}
    m = privacy.mask(obj)
    psn = plate_pseudonym("UP32AB1234")
    assert m["plate_text"] == psn and m["nested"][0]["plate_raw"] == psn
    assert "UP32AB1234" not in m["title"] and psn in m["title"]
    assert "plate_reads_raw" not in m and m["count"] == 3
    assert privacy.pseudonym_prefix(psn) == psn[4:].lower()


def _user(role: str) -> CurrentUser:
    return CurrentUser(id=1, username=role, role=role, full_name="", permissions=set(ROLE_PERMISSIONS[role]))


def test_ws_filter_masks_and_enforces_permissions() -> None:
    ev = ui_event("vehicle_matched", {"plate_text": "UP32AB1234", "camera_id": "A", "sensitive": True})
    op = filter_event(ev, _user("operator"), True, None)
    assert op is not None and op["data"]["plate_text"] == "UP32AB1234" and "sensitive" not in op["data"]
    an = filter_event(ev, _user("analyst"), False, None)
    assert an is not None and an["data"]["plate_text"].startswith("PSN-")
    assert filter_event(ev, _user("viewer"), False, None) is None  # no trajectory permission
    assert filter_event(ui_event("camera_status_changed", {"camera_id": "A"}), _user("viewer"), False, None) is not None
    assert filter_event(ev, _user("operator"), True, {"alert_created"}) is None  # subscription filter
    assert filter_event(ui_event("internal_thing", {}), _user("admin"), True, None) is None  # unknown types never leak


def test_websocket_rejects_bad_token_and_streams_events(client, admin, analyst, bus) -> None:  # noqa: ANN001
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws/events?token=bogus") as ws:
        assert ws.receive_json()["type"] == "error"
        ws.receive_json()
    assert exc.value.code == 4401
    token = analyst["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/events?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["data"]["raw_plates"] is False
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        bus.publish_ui(ui_event("plate_read", {"plate_text": "UP32AB1234", "camera_id": "A", "sensitive": True}))
        msg = ws.receive_json()
        assert msg["type"] == "plate_read" and msg["data"]["plate_text"].startswith("PSN-")


# ------------------------------------------------------------------ ONVIF
PROBE_MATCH = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches><d:ProbeMatch>
 <a:EndpointReference><a:Address>urn:uuid:1234</a:Address></a:EndpointReference>
 <d:Scopes>onvif://www.onvif.org/name/Junction_Cam onvif://www.onvif.org/hardware/IPC-9000 onvif://www.onvif.org/location/Hazratganj</d:Scopes>
 <d:XAddrs>http://192.0.2.44/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""


def test_onvif_probe_match_parsing() -> None:
    [dev] = onvif.parse_probe_match(PROBE_MATCH)
    assert dev["host"] == "192.0.2.44" and dev["name"] == "Junction Cam" and dev["hardware"] == "IPC-9000"
    assert onvif.parse_probe_match(b"not xml") == []


def _soap(body: str) -> bytes:
    return (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            f'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{body}</s:Body></s:Envelope>').encode()


def test_onvif_client_against_mock_device() -> None:
    replies = {
        "GetDeviceInformation": _soap("<tds:GetDeviceInformationResponse><tds:Manufacturer>Acme</tds:Manufacturer><tds:Model>X1</tds:Model>"
                                      "</tds:GetDeviceInformationResponse>"),
        "GetCapabilities": _soap("<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>http://192.0.2.44/onvif/media</tt:XAddr>"
                                 "</tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"),
        "GetProfiles": _soap('<trt:GetProfilesResponse><trt:Profiles token="main"><tt:Name>Main</tt:Name><tt:VideoEncoderConfiguration>'
                             "<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
                             "<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl></tt:VideoEncoderConfiguration>"
                             "</trt:Profiles></trt:GetProfilesResponse>"),
        "GetStreamUri": _soap("<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://192.0.2.44:554/main</tt:Uri></trt:MediaUri>"
                              "</trt:GetStreamUriResponse>"),
    }
    sent = []

    def fake_post(url: str, content: bytes, **_: object) -> SimpleNamespace:
        text = content.decode()
        sent.append(text)
        op = next(k for k in replies if k in text)
        return SimpleNamespace(status_code=200, content=replies[op])

    with mock.patch.object(onvif.httpx, "post", side_effect=fake_post):
        res = onvif.OnvifClient("http://192.0.2.44/onvif/device_service", "admin", "cam-secret").probe()
    assert res["device"]["Manufacturer"] == "Acme"
    assert res["profiles"][0]["resolution"] == "1920x1080" and res["profiles"][0]["rtsp_uri"] == "rtsp://192.0.2.44:554/main"
    assert "cam-secret" not in str(res)
    assert all("cam-secret" not in s for s in sent), "WS-Security must send a digest, never the password"

    fault = _soap("<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason><s:Detail>ter:NotAuthorized</s:Detail></s:Fault>")
    with mock.patch.object(onvif.httpx, "post", return_value=SimpleNamespace(status_code=400, content=fault)):
        try:
            onvif.OnvifClient("http://192.0.2.44/onvif/device_service", "admin", "bad").device_information()
            raise AssertionError("expected OnvifError")
        except onvif.OnvifError as e:
            assert "authentication failed" in str(e)


# ------------------------------------------------------------------ rate limiting
def test_sliding_window_limiter() -> None:
    lim = SlidingWindowLimiter(3, window_s=10)
    assert [lim.check("k", now=100 + i)[0] for i in range(4)] == [True, True, True, False]
    assert lim.check("other", now=103)[0] is True
    assert lim.check("k", now=111)[0] is True  # the window slid


def test_worker_shards_partition_cameras():
    import pytest

    from app.workers.manager import owns_camera, parse_shard

    ids = [f"CAM-{i:02d}" for i in range(1, 41)]
    shards = [parse_shard(f"{i}/3") for i in range(3)]
    owners = [[s for s in shards if owns_camera(c, s)] for c in ids]
    assert all(len(o) == 1 for o in owners)  # every camera runs on exactly one worker
    assert all(any(owns_camera(c, s) for c in ids) for s in shards)
    assert all(owns_camera(c, parse_shard("0/1")) for c in ids)
    with pytest.raises(ValueError):
        parse_shard("3/3")


def test_plate_veto_separates_lookalike_registrations_but_not_misreads() -> None:
    bar = _defaults()["identity"]["plate_min_confidence"]

    def veto(a: str, b: str, conf_a: float, conf_b: float) -> str | None:
        return plate_veto(a, b, min(conf_a, conf_b) / bar)

    # false merges seen in a live demo run (checked against the scenario's ground truth)
    assert veto("UP14JR7670", "UP14JX9670", 1.0, 1.0) == "two confident plate reads disagree"
    assert veto("UP65A0417", "UP65A9415", 0.99, 1.0) == "two confident plate reads disagree"
    assert veto("UP32TE4006", "UP32TE6006", 0.97, 0.97) == "two confident plate reads disagree"  # sequential plates
    assert veto("UP32B5796", "UP32TT404", 1.0, 0.58) == "plate reads disagree on most characters"
    assert veto("UP32B4365", "UP32TC6489", 0.38, 1.0) == "plate reads disagree on most characters"
    # OCR near-misses of one plate stay linkable
    assert veto("UP32TE7007", "UP32TE7O07", 1.0, 0.97) is None  # confusable O/0
    assert veto("UP32X209", "UP32X2093", 0.75, 0.58) is None  # dropped character, weak read
    # a read far below the bar carries no veto: it may be OCR noise
    assert veto("UP32B5796", "HR55GT8073", 1.0, 0.2) is None
