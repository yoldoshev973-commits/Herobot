"""QR havolasini to'liq dekodlash (faqat O'QIYDI — hech narsa yaratmaydi/yubormaydi).

Hero QR havolasi odatda `...?url=<base64(JSON)>` ko'rinishida bo'ladi. Muhim: query qo'lda ajratiladi va
`unquote` (unquote_plus EMAS) ishlatiladi — `parse_qs` base64 dagi '+' ni bo'shliqqa aylantirib, dekodlashni buzardi.
"""
import base64
import binascii
import json
import re
from urllib.parse import urlsplit, unquote

_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MAX_DEPTH = 4

# Kalit nomi variantlari (kichik harf, '_' va '-' siz solishtiriladi)
_META_KEYS = {
    "subject": ("subject", "subjectname", "lesson", "lessonname"),
    "stream_id": ("streamid", "steamid"),
    "event_type": ("eventtype",),
    "begin_time": ("begintime", "begin", "starttime", "start"),
    "end_time": ("endtime", "end", "finishtime"),
}


def split_query(query):
    """'a=1&b=x+y%2Bz' -> {'a': '1', 'b': 'x+y+z'}; '+' o'zgarmaydi, %XX dekodlanadi. Takrorlanuvchi kalit -> ro'yxat."""
    out = {}
    for part in (query or "").split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        k, v = unquote(k), unquote(v)
        if k in out:
            cur = out[k]
            out[k] = (cur if isinstance(cur, list) else [cur]) + [v]
        else:
            out[k] = v
    return out


def b64_decode_text(s):
    """Base64 (oddiy +/ yoki urlsafe -_ , padding bo'lmasa ham, %-kodlangan bo'lsa ham) -> matn; bo'lmasa None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) < 4:
        return None
    if "%" in s:
        s = unquote(s)
    s = s.replace(" ", "+").replace("-", "+").replace("_", "/")   # ' ' — '+' allaqachon bo'shliqqa aylangan bo'lsa
    if not _B64_RE.match(s):
        return None
    core = s.rstrip("=")
    if len(core) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(core + "=" * (-len(core) % 4))
        text = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not text or not all(c.isprintable() or c in "\r\n\t" for c in text):
        return None
    return text


def _try_json(text):
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, (dict, list)) else None


def _expand(value, depth=0):
    """Qiymat ichidagi base64/JSON qatlamlarini ochadi (ichma-ich 'data' ham)."""
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _expand(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, depth + 1) for v in value]
    if isinstance(value, str):
        t = value.strip()
        if t[:1] in "{[":
            j = _try_json(t)
            if j is not None:
                return _expand(j, depth + 1)
        txt = b64_decode_text(t) if len(t) >= 8 else None
        if txt is not None:
            j = _try_json(txt)
            if j is not None:
                return _expand(j, depth + 1)
    return value


def _scalar(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False)


def flatten_fields(obj, prefix="", out=None):
    """JSON -> {'kalit': qiymat}. 'data' obyekti ochiladi (prefikssiz); to'qnashuvda 'data.kalit' bo'ladi."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "data" and isinstance(v, dict):
                inner = {}
                flatten_fields(v, prefix, inner)
                for ik, iv in inner.items():
                    out[ik if ik not in out else f"data.{ik}"] = iv
            elif isinstance(v, dict):
                flatten_fields(v, f"{prefix}{k}.", out)
            else:
                out[f"{prefix}{k}"] = _scalar(v)
    return out


def _norm(k):
    return re.sub(r"[_\-\s]", "", str(k).split(".")[-1]).lower()


def _pick_meta(fields):
    by_norm = {}
    for k, v in fields.items():
        if v in (None, ""):
            continue
        by_norm.setdefault(_norm(k), v)
    meta = {}
    for name, aliases in _META_KEYS.items():
        meta[name] = next((by_norm[a] for a in aliases if a in by_norm), None)
    return meta


def to_epoch_sec(v):
    """Soniya yoki millisoniya -> soniya; bo'lmasa None."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n // 1000 if n > 10_000_000_000 else n


def decode_qr_full(url):
    """QR matni -> {scheme, host, path, params, decoded:[...], fields, meta}. URL bo'lmasa ham xatosiz qaytadi."""
    res = {"scheme": "", "host": "", "path": "", "params": {}, "decoded": [], "fields": {},
           "meta": {k: None for k in _META_KEYS}}
    res["meta"].update(begin_sec=None, end_sec=None)
    try:
        url = str(url or "").strip()
        sp = urlsplit(url)
        if not (sp.scheme and sp.netloc):
            return res
        res.update(scheme=sp.scheme, host=sp.hostname or "", path=sp.path or "")
        params = split_query(sp.query)
        if sp.fragment and "=" in sp.fragment:
            frag_q = sp.fragment.split("?", 1)[-1]
            for k, v in split_query(frag_q).items():
                params.setdefault(k, v)
        res["params"] = params
        fields = {}
        for name, val in params.items():
            for v in (val if isinstance(val, list) else [val]):
                txt, enc = None, None
                t = (v or "").strip()
                if t[:1] in "{[" and _try_json(t) is not None:
                    txt, enc = t, "json"
                else:
                    txt = b64_decode_text(t)
                    enc = "base64" if txt is not None else None
                if txt is None:
                    continue
                j = _try_json(txt)
                item = {"param": name, "encoding": enc, "text": txt, "json": None, "fields": {}}
                if j is not None:
                    j = _expand(j)
                    item["json"] = j
                    item["fields"] = flatten_fields(j) if isinstance(j, dict) else {}
                elif txt.startswith(("http://", "https://")):   # ichida yana havola bo'lsa — uni ham ochamiz
                    inner = decode_qr_full(txt) if txt != url else None
                    if inner:
                        item["fields"] = {**{f"param.{k}": v for k, v in inner["params"].items() if not isinstance(v, list)}, **inner["fields"]}
                res["decoded"].append(item)
                for fk, fv in item["fields"].items():
                    fields.setdefault(fk, fv)
        res["fields"] = fields
        meta = _pick_meta(fields)
        meta["begin_sec"], meta["end_sec"] = to_epoch_sec(meta["begin_time"]), to_epoch_sec(meta["end_time"])
        res["meta"] = meta
    except Exception:
        pass
    return res
