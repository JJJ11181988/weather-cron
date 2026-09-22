#!/usr/bin/env python3
# weather_hub.py —— 多源天气融合（和风天气 首选 + nmc 国家预警）· 通用参数化版
# 设计：实况/风力/紫外线/分钟级/预警均以和风为主；官方预警用 nmc 交叉校验。
#        Open-Meteo 不再参与播报（用户要求以和风为首选、播报中不使用 Open-Meteo 数据）。
#        空气质量改用 WAQI(aqicn.org) 免费专业源（和风空气质量接口在本套餐/Host 返回 404）。
# 依赖：仅标准库；和风凭据走环境变量 QW_HOST / QW_KEY；空气质量走 WAQI_TOKEN
# 用法：
#   python weather_hub.py                             # 用默认位置（取自环境变量）
#   python weather_hub.py --name 北京                 # 城市名 -> 经纬度(和风 GeoAPI)
#   python weather_hub.py --lat 39.90 --lon 116.41 --adcode 110101
# 位置也可通过环境变量 WEATHER_LAT / WEATHER_LON / WEATHER_ADCODE / WEATHER_KEYWORD 注入
import argparse, json, os, re, sys, time, gzip, urllib.request, urllib.parse

# 凭据从 ~/.workbuddy/weather.secrets 或环境变量读取（避免明文入库；云端由 GitHub Secrets 注入环境变量）
_SECRETS_FILE = os.path.expanduser("~/.workbuddy/weather.secrets")
if os.path.exists(_SECRETS_FILE):
    with open(_SECRETS_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

QW_HOST = os.environ.get("QW_HOST", "")
QW_KEY  = os.environ.get("QW_KEY", "")
WAQI_TOKEN = os.environ.get("WAQI_TOKEN", "")   # aqicn.org 免费空气质量 token


def _env_float(key):
    v = os.environ.get(key)
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


# 位置一律从环境变量读取（本地 ~/.workbuddy/weather.secrets，云端 GitHub Secrets）。
# 【重要】切勿把坐标/adcode 硬编码回源码：仓库 public 时会暴露精确到 ~1km 的住址。
DEFAULT_LAT = _env_float("WEATHER_LAT")
DEFAULT_LON = _env_float("WEATHER_LON")
DEFAULT_ADCODE = os.environ.get("WEATHER_ADCODE") or None
DEFAULT_KEYWORD = os.environ.get("WEATHER_KEYWORD") or None

THUNDER_ICONS = {302, 303, 304}      # 和风图标：雷阵雨 / 强雷阵雨 / 雷阵雨伴冰雹
BLOCK_LEVELS_NMC = {"红色", "橙色"}
QW_SEV_BLOCK = {"severe", "extreme"}  # 和风：橙(severe)/红(extreme)


def _get_json(url, headers=None, timeout=20):
    h = {"User-Agent": "curl/8", "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def geocode(name):
    # 和风 GeoAPI（中国区县名支持好）
    if QW_HOST and QW_KEY:
        try:
            d = qweather(f"/geo/v2/city/lookup?location={urllib.parse.quote(name)}")
            loc = d.get("location", [])
            if loc:
                r = loc[0]
                return float(r["lat"]), float(r["lon"]), r.get("name", name)
        except Exception:
            pass
    raise RuntimeError(f"地理编码未找到: {name}（可改用 --lat/--lon 直接指定坐标）")


def qweather(path):
    if not QW_HOST or not QW_KEY:
        raise RuntimeError("缺少环境变量 QW_HOST / QW_KEY（和风专属 Host 与 API KEY）")
    return _get_json(f"https://{QW_HOST}{path}", headers={"X-QW-Api-Key": QW_KEY})


def qw_now(lat, lon):
    return qweather(f"/v7/weather/now?location={lon},{lat}")


def qw_minutely(lat, lon):
    return qweather(f"/v7/minutely/5m?location={lon},{lat}")


def qw_alert(lat, lon):
    return qweather(f"/weatheralert/v1/current/{lat}/{lon}")


def qw_indices(lat, lon, typ=5):
    """和风紫外线指数（type=5 紫外线）当日值，返回 {level, category, value, text}"""
    d = qweather(f"/v7/indices/1d?type={typ}&location={lon},{lat}")
    daily = d.get("daily") or []
    if not daily:
        raise RuntimeError("和风 indices 返回为空")
    return daily[0]


def waqi_air(lat, lon):
    """WAQI(aqicn.org) 免费空气质量：按经纬度自动匹配最近国控站点。
    返回 {aqi, level, pm25, pm10, o3, no2, dominentpol, station, time, source}
    """
    if not WAQI_TOKEN:
        raise RuntimeError("缺少环境变量 WAQI_TOKEN（aqicn.org 免费 token）")
    d = _get_json(f"https://api.waqi.info/feed/geo:{lat};{lon}/?token={WAQI_TOKEN}")
    if d.get("status") != "ok":
        raise RuntimeError(f"WAQI 返回异常: {d.get('status')}")
    data = d.get("data") or {}
    iaqi = data.get("iaqi") or {}
    try:
        aqi = int(data.get("aqi"))
    except (TypeError, ValueError):
        aqi = None          # 部分站点 aqi 返回 "-"，视为无数据
    # 站点名清理：WAQI 原始形如 "东莞麻涌,  (东莞市东莞麻涌)"，取简洁中文名
    st_raw = (data.get("city") or {}).get("name") or ""
    st = st_raw.split(",")[0].strip()
    if "(" in st:
        st = st.split("(")[0].strip()
    if st and all(ord(c) < 128 for c in st):    # 全拼音时改取括号内的中文
        m = re.search(r"[（(]([^（）()]*[一-鿿][^（）()]*)[）)]", st_raw)
        if m:
            st = m.group(1)
    return {
        "aqi": aqi,
        "level": aqi_level(aqi),
        "pm25": (iaqi.get("pm25") or {}).get("v"),
        "pm10": (iaqi.get("pm10") or {}).get("v"),
        "o3": (iaqi.get("o3") or {}).get("v"),
        "no2": (iaqi.get("no2") or {}).get("v"),
        "dominentpol": (data.get("dominentpol") or "").upper() or None,
        "station": st,
        "time": (data.get("time") or {}).get("s"),
        "source": "WAQI",
    }


def aqi_level(aqi):
    """中国环境空气质量指数分级（HJ 633-2012）"""
    if aqi is None:
        return "未知"
    if aqi <= 50:  return "优"
    if aqi <= 100: return "良"
    if aqi <= 150: return "轻度污染"
    if aqi <= 200: return "中度污染"
    if aqi <= 300: return "重度污染"
    return "严重污染"


def uv_level(uv):
    """中国气象 UV 等级换算（基于指数值）"""
    if uv is None:
        return None
    if uv < 3: return "最弱"
    if uv < 5: return "弱"
    if uv < 7: return "中等"
    if uv < 10: return "强"
    return "很强"


def report_time():
    """北京时间格式化时间串（云端 runner 为 UTC，需 +8h）"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


def nmc_alerts(adcode=None, keyword=None, top=5):
    url = "https://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=50"
    data = _get_json(url)
    inner = data.get("data", {})
    if "list" in inner:
        items = inner["list"]
    elif "page" in inner and "list" in inner.get("page", {}):
        items = inner["page"]["list"]
    else:
        items = []
    out = []
    for a in items:
        if adcode and a["alertid"].startswith(adcode):
            out.append(a)
        elif keyword and keyword in a["title"]:
            out.append(a)
    return out[:top]


def decide(lat=DEFAULT_LAT, lon=DEFAULT_LON, adcode=None, keyword=None):
    report = {"time": report_time(),
              "location": {"lat": lat, "lon": lon, "adcode": adcode, "keyword": keyword},
              "sources": {}}
    reasons, run = [], True

    # 实况：和风（首选，字段齐全：text/precip/temp/humidity/wind）
    try:
        qn = qw_now(lat, lon)
        now = qn.get("now", {})
        text = now.get("text", "")
        try:
            precip = float(now.get("precip") or 0)
        except (ValueError, TypeError):
            precip = 0.0
        try:
            icon = int(now.get("icon") or 0)
        except (ValueError, TypeError):
            icon = 0
        report["sources"]["qw_now"] = {
            "text": text, "temp": now.get("temp"), "humidity": now.get("humidity"),
            "precip": precip, "icon": icon}
        if "雷" in text or icon in THUNDER_ICONS:
            run, reasons = False, reasons + [f"和风实况为雷暴({text})"]
        if precip > 0:
            reasons.append(f"当前有降水({precip}mm)")
        report["sources"]["qw_wind"] = {
            "dir": now.get("windDir"), "scale": now.get("windScale"),
            "speed": now.get("windSpeed")}
    except Exception as e:
        report["sources"]["qw_now"] = {"error": str(e)}

    # 官方预警：nmc
    try:
        alerts = nmc_alerts(adcode, keyword)
        report["sources"]["nmc_alert"] = [a["title"] for a in alerts if "解除" not in a["title"]]
        for a in alerts:
            if any(lv in a["title"] for lv in BLOCK_LEVELS_NMC):
                run, reasons = False, reasons + [f"官方预警：{a['title']}"]
    except Exception as e:
        report["sources"]["nmc_alert"] = {"error": str(e)}

    # 和风预警（交叉验证）
    try:
        aw = qw_alert(lat, lon)
        alerts = aw.get("alerts") or aw.get("warning") or []
        titles = []
        for w in alerts:
            h = w.get("headline", "")
            if "解除" in h:   # 跳过已撤销(解除)类预警，仅保留生效中
                continue
            titles.append(h)
            if (w.get("severity") or "").lower() in QW_SEV_BLOCK:
                run, reasons = False, reasons + [f"和风预警：{h}"]
        report["sources"]["qw_alert"] = titles
    except Exception as e:
        report["sources"]["qw_alert"] = {"error": str(e)}

    # 分钟级降水
    try:
        m = qw_minutely(lat, lon)
        series = m.get("minutely", [])[:6]
        rain_soon = any(float(x.get("precip") or 0) > 0.05 for x in series)
        report["sources"]["qw_minutely"] = {
            "summary": m.get("summary"), "rain_next30min": rain_soon}
        if rain_soon and run:
            reasons.append("未来30分钟有零星降水（雨势将减弱）")
    except Exception as e:
        report["sources"]["qw_minutely"] = {"error": str(e)}

    # 空气质量：WAQI(aqicn.org) 免费专业源（和风空气质量接口在本套餐/Host 404）
    try:
        a = waqi_air(lat, lon)
        aqi = a.get("aqi")
        report["sources"]["air_quality"] = a
        if isinstance(aqi, int):
            if aqi >= 201:                      # 重度及以上：停户外
                run, reasons = False, reasons + [f"空气质量AQI {aqi}({a['level']})"]
            elif aqi >= 151:                    # 中度：建议大幅缩短/改室内
                reasons.append(f"空气质量AQI {aqi}({a['level']})，建议缩短时长或改室内")
            elif aqi >= 101:                    # 轻度：敏感人群减量
                reasons.append(f"空气质量AQI {aqi}({a['level']})，建议降低运动强度")
    except Exception as e:
        report["sources"]["air_quality"] = {"error": str(e)}

    # 紫外线：和风 indices
    uv = {"level": None, "category": None, "source": "qweather"}
    try:
        idx = qw_indices(lat, lon)
        uv.update({"level": idx.get("level"), "category": idx.get("category")})
    except Exception as e:
        report["sources"]["uv_index"] = {"qweather_error": str(e)}
    report["sources"]["uv_index"] = uv

    report["decision"] = "GO" if run else "NO-GO"
    report["reasons"] = reasons
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="多源天气融合查询（和风首选）")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--adcode", default=None, help="区县行政区划码(6位)，用于 nmc 精确过滤")
    p.add_argument("--name", help="城市/地名，用于地理编码与 nmc 标题匹配")
    p.add_argument("--keyword", help="nmc 预警标题匹配关键词（默认取 name）")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    lat, lon = a.lat, a.lon
    if lat is None or lon is None:
        if a.name:
            lat, lon, _ = geocode(a.name)
        else:
            lat, lon = DEFAULT_LAT, DEFAULT_LON
    if lat is None or lon is None:
        raise RuntimeError("未指定位置：请传 --lat/--lon 或 --name，"
                           "或设置环境变量 WEATHER_LAT/WEATHER_LON")
    keyword = a.keyword or a.name or DEFAULT_KEYWORD
    return decide(lat, lon, adcode=(a.adcode or DEFAULT_ADCODE), keyword=keyword)


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
