#!/usr/bin/env python3
# alert_watch.py —— 灾害预警监控：发现「新发/升级」的橙/红预警时企业微信推送
# 逻辑：拉和风预警(首选)+nmc(校验) → 提取橙/红级别 → 与上次状态(.alert_state.json)对比
#       → 有新增/升级才推送（解除不推送，避免打扰）→ 保存新状态
# 用法同 weather_hub.py：--name / --lat / --lon / --adcode / --keyword
# 状态文件由云端 workflow 提交回仓库持久化；本地测试可用 WEATHER_DRY_RUN=1
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weather_hub
from notify_weather import push_all, _log

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".alert_state.json")
ORANGE_RED = {"橙色", "红色"}


def collect_alerts(lat, lon, adcode, keyword):
    """返回当前生效的橙/红预警 [{key, level, headline, source}]，双源去重（同 key 只留一条）"""
    items, seen = [], set()

    # 首选：和风预警（severity severe=橙 / extreme=红）
    try:
        aw = weather_hub.qw_alert(lat, lon)
        for w in (aw.get("alerts") or aw.get("warning") or []):
            sev = (w.get("severity") or "").lower()
            if sev in weather_hub.QW_SEV_BLOCK:
                key = w.get("id") or f"qw:{w.get('headline')}:{w.get('effectiveTime')}"
                if key not in seen:
                    seen.add(key)
                    items.append({"key": key, "level": "红色" if sev == "extreme" else "橙色",
                                  "headline": w.get("headline", ""), "source": "和风"})
    except Exception as e:
        print("和风预警读取失败:", e)

    # 校验：nmc 官方（标题含 橙/红）
    try:
        for a in weather_hub.nmc_alerts(adcode, keyword, top=20):
            if any(lv in a["title"] for lv in ORANGE_RED):
                key = f"nmc:{a['alertid']}"
                if key not in seen:
                    seen.add(key)
                    items.append({"key": key,
                                  "level": "红色" if "红色" in a["title"] else "橙色",
                                  "headline": a["title"], "source": "官方"})
    except Exception as e:
        print("nmc 预警读取失败:", e)
    return items


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"keys": []}


def main(argv=None):
    p = argparse.ArgumentParser(description="灾害预警监控")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--adcode", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--keyword", default=None)
    a = p.parse_args(argv)

    lat, lon = a.lat, a.lon
    if lat is None or lon is None:
        if a.name:
            lat, lon, _ = weather_hub.geocode(a.name)
        else:
            lat, lon = weather_hub.DEFAULT_LAT, weather_hub.DEFAULT_LON
    if lat is None or lon is None:
        raise RuntimeError("未指定位置：请传 --lat/--lon 或 --name，"
                           "或设置环境变量 WEATHER_LAT/WEATHER_LON")
    keyword = a.keyword or a.name or weather_hub.DEFAULT_KEYWORD
    adcode = a.adcode or weather_hub.DEFAULT_ADCODE

    items = collect_alerts(lat, lon, adcode, keyword)
    new_keys = {i["key"] for i in items}
    old_keys = set(load_state().get("keys", []))
    added = [i for i in items if i["key"] not in old_keys]

    # 保存新状态（无论有无变化都更新，避免解除后残留旧 key）
    with open(STATE_FILE, "w") as f:
        json.dump({"keys": sorted(new_keys), "updated": weather_hub.report_time()}, f, ensure_ascii=False)

    if not added:
        _log(f"预警监控: 当前橙/红预警 {len(items)} 条，无新增/升级，不推送")
        return 0

    lines = [f"## ⚠️ 灾害预警 · {len(added)} 条新增/升级", ""]
    for i in added:
        lines.append(f"- **{i['level']}** {i['headline']}（{i['source']}）")
    lines.append("")
    lines.append(f"检查时间: {weather_hub.report_time()}")
    lines.append("详情以官方发布为准，注意防范。")

    content = "\n".join(lines)
    _log(content)
    resp = push_all(content)
    if resp is None:
        print("预警推送失败：请检查 WECOM_WEBHOOK_URL 或自建应用凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
