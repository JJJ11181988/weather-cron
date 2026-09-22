#!/usr/bin/env python3
# notify_weather.py —— 拉和风天气(首选) + nmc 官方预警 + WAQI 空气质量 → 判定 GO/NO-GO → 企业微信推送
#
# 推送通道（双实现，按可用凭据自动选择；设计目标：100% 云端运行）：
#   [主] 群机器人 webhook  → 企业微信 App。不需要 access_token，**不校验「企业可信IP」**，
#        可从任意 IP（含 GitHub Actions 动态 IP）直接调用。云端唯一可行通道。
#   [备] 自建应用消息      → 经「微信插件」转发到个人微信。但 2022-06 起新创建的自建应用
#        **必须配置「企业可信IP」且禁止填第三方服务商 IP**，故 GitHub Actions 等云端环境
#        调用必然返回 60020。仅在本地(家庭固定IP已加白名单)环境可用。
# 播报数据源：和风（实况/风力/紫外线/分钟级/预警）+ nmc(官方预警校验) + WAQI(空气质量)；
#             Open-Meteo 不进入播报。
# 灾害天气 / 空气污染时自动追加防御与健康建议
# 依赖同目录 weather_hub.py；用法同 weather_hub.py（支持 --name/--lat/--lon/--adcode）
#
# 所需环境变量（本地放 ~/.workbuddy/weather.secrets，云端放 GitHub Secrets）：
#   WECOM_WEBHOOK_URL 群机器人 webhook（云端主通道，推荐配置）
#   WECOM_CORPID      企业 ID（本地自建应用通道用）
#   WECOM_APP_SECRET  自建应用 Secret（本地自建应用通道用）
#   WECOM_AGENTID     自建应用 AgentId
#   WECOM_TOUSER      接收人 userid，默认 @all（需在企业微信内且属于应用可见范围）
#   WEATHER_QUIET=1   云端静默模式：不打印天气明细，避免公开日志泄露位置
import os, sys, json, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weather_hub

WECOM_WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "")
WECOM_CORPID = os.environ.get("WECOM_CORPID", "")
WECOM_APP_SECRET = os.environ.get("WECOM_APP_SECRET", "")
WECOM_AGENTID = os.environ.get("WECOM_AGENTID", "")
WECOM_TOUSER = os.environ.get("WECOM_TOUSER", "@all")
QUIET = os.environ.get("WEATHER_QUIET") == "1"


def _log(*args):
    """静默模式(WEATHER_QUIET=1)下不打印天气明细，避免公开日志泄露位置。"""
    if not QUIET:
        print(*args)


def _wecom_get_token():
    """获取企业微信 access_token（有效期 7200s；本系统调用频率极低，按次获取即可）。"""
    if not (WECOM_CORPID and WECOM_APP_SECRET):
        print("未设置 WECOM_CORPID / WECOM_APP_SECRET，跳过企微应用推送")
        return None
    url = (f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
           f"?corpid={WECOM_CORPID}&corpsecret={WECOM_APP_SECRET}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("errcode") == 0:
            return d.get("access_token")
        print("企业微信获取 access_token 失败:", d)
        return None
    except Exception as e:
        print("企业微信获取 access_token 异常:", e)
        return None


def _truncate(s, limit=1900):
    """自建应用 markdown 内容上限 2048 字节，按字节安全截断（不切断多字节字符）。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    b = b[:limit]
    while b and (b[-1] & 0xC0) == 0x80:  # 去掉被截断的 UTF-8 续字节
        b = b[:-1]
    return b.decode("utf-8", "ignore") + "…"


def wecom_app_push(content, dry_run=None):
    """企业微信自建应用推送（markdown）。单通道主推送，经微信插件转发到个人微信。"""
    if dry_run is None:
        dry_run = os.environ.get("WEATHER_DRY_RUN") == "1"
    if dry_run:
        print(f"[DRY_RUN] 跳过企微自建应用推送: {content.splitlines()[0] if content else ''}")
        return '{"errcode":0,"errmsg":"dry_run"}'
    token = _wecom_get_token()
    if not token:
        return None
    try:
        agentid = int(WECOM_AGENTID) if WECOM_AGENTID else 0
    except ValueError:
        agentid = 0
    body = {
        "touser": WECOM_TOUSER,
        "msgtype": "markdown",
        "agentid": agentid,
        "markdown": {"content": _truncate(content)},
    }
    data = json.dumps(body).encode("utf-8")
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        print("企业微信应用推送异常:", e)
        return None


def wecom_webhook_push(content, dry_run=None):
    """群机器人 webhook 推送（markdown）。云端主通道：无需 access_token，不校验「企业可信IP」。"""
    if dry_run is None:
        dry_run = os.environ.get("WEATHER_DRY_RUN") == "1"
    if not WECOM_WEBHOOK_URL:
        _log("未设置 WECOM_WEBHOOK_URL，跳过群机器人推送")
        return None
    if dry_run:
        _log(f"[DRY_RUN] 跳过群机器人推送: {content.splitlines()[0] if content else ''}")
        return '{"errcode":0,"errmsg":"dry_run"}'
    body = {"msgtype": "markdown", "markdown": {"content": _truncate(content, 4096)}}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(WECOM_WEBHOOK_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        print("群机器人推送异常:", e)
        return None


def push_all(content, dry_run=None):
    """统一推送入口：优先群机器人(云端可用)，失败或未配置时回退自建应用(需可信IP)。"""
    resp = wecom_webhook_push(content, dry_run=dry_run)
    if resp is not None:
        print("WeComWebhook:", resp)
        return resp
    resp = wecom_app_push(content, dry_run=dry_run)
    if resp is not None:
        print("WeComApp:", resp)
    return resp


def disaster_advice(thunder, alert_texts):
    joined = "".join(alert_texts)
    out = []
    if thunder or "雷电" in joined or "雷雨" in joined:
        out.append("雷电：远离空旷高地、大树、金属物，尽快进入室内避雷")
    if "暴雨" in joined:
        out.append("暴雨：避开低洼积水路段，远离河道山坡，警惕城乡积涝与地质灾害滞后性")
    if "大风" in joined or "台风" in joined:
        out.append("大风/台风：远离广告牌、临时搭建物，关好门窗，减少外出")
    if "冰雹" in joined:
        out.append("冰雹：尽量在室内避险，车辆停入车库或加盖防护")
    if "高温" in joined:
        out.append("高温：避免正午高温时段运动，及时补水，注意防暑")
    return out


def uv_advice(uv_value, category):
    cat = category or ""
    if ("强" in cat) or (uv_value is not None and uv_value >= 7):
        idx = f"指数{uv_value}" if uv_value is not None else ""
        tip = f"紫外线{cat}({idx})：涂抹防晒霜，戴帽/墨镜，避免10-14点长时间暴晒".replace("()", "")
        return [tip]
    return []


def aqi_advice(aqi, level):
    """空气污染健康建议（依据 HJ 633-2012 分级），AQI<=100 不输出。"""
    if not isinstance(aqi, int):
        return []
    if aqi >= 201:
        return [f"空气AQI {aqi}({level})：停止户外运动，关窗并开启净化设备，外出佩戴口罩"]
    if aqi >= 151:
        return [f"空气AQI {aqi}({level})：建议改室内或大幅缩短时长，易感人群避免户外运动"]
    if aqi >= 101:
        return [f"空气AQI {aqi}({level})：可做低强度活动，避免长跑等剧烈运动，呼吸道不适即停"]
    return []


def wind_advice(scale, gusts):
    try:
        sc = int(str(scale).replace("级", "").strip()) if scale else 0
    except (ValueError, TypeError):
        sc = 0
    if sc >= 6 or (gusts and float(gusts) >= 39):
        return [f"风力较大(约{scale or '?'}级)：户外运动注意防风，远离高空坠物风险区"]
    return []


def main(argv=None):
    r = weather_hub.main(argv)
    go = r["decision"] == "GO"
    s = r["sources"]
    lines = []

    # 实况：和风
    qn = s.get("qw_now")
    thunder = False
    if isinstance(qn, dict) and "error" not in qn:
        text = qn.get("text", "")
        precip = qn.get("precip")
        lines.append(f"实况: {text} 降水{precip}mm 气温{qn.get('temp')}°C 湿度{qn.get('humidity')}%")
        thunder = "雷" in text
    elif isinstance(qn, dict) and "error" in qn:
        lines.append(f"实况: 和风获取失败({qn['error']})")

    # 风力：和风
    qw = s.get("qw_wind")
    if isinstance(qw, dict) and qw.get("scale"):
        ws = f" 风速{qw.get('speed')}km/h" if qw.get("speed") else ""
        lines.append(f"风力: {qw.get('dir','')}{qw.get('scale')}级{ws}")

    # 紫外线：和风
    uv = s.get("uv_index")
    if isinstance(uv, dict) and uv.get("category"):
        lv = f"({uv['level']})" if uv.get("level") else ""
        lines.append(f"紫外线: 当日{uv['category']}{lv}")

    alert_texts = []
    nmc = s.get("nmc_alert")
    if isinstance(nmc, list) and nmc:
        alert_texts += nmc
        lines.append("官方预警: " + "；".join(nmc))
    qa = s.get("qw_alert")
    if isinstance(qa, list) and qa:
        alert_texts += qa
        lines.append("和风预警: " + "；".join(qa))

    qm = s.get("qw_minutely")
    if isinstance(qm, dict):
        lines.append(f"分钟级降水: {qm.get('summary')}")

    aq = s.get("air_quality")
    if isinstance(aq, dict) and aq.get("aqi") is not None:
        seg = [f"空气质量: AQI {aq['aqi']}({aq['level']})"]
        if aq.get("pm25") is not None:
            seg.append(f"PM2.5 {aq['pm25']}")
        if aq.get("pm10") is not None:
            seg.append(f"PM10 {aq['pm10']}")
        if aq.get("dominentpol"):
            seg.append(f"首要污染物 {aq['dominentpol']}")
        lines.append(" ".join(seg))
        st, tm = aq.get("station") or "附近站点", aq.get("time") or ""
        lines.append(f"  站点: {st}（源:WAQI {tm}）")
    elif isinstance(aq, dict) and aq.get("error"):
        lines.append(f"空气质量: 获取失败({aq['error']})")

    if r["reasons"]:
        lines.append("判定依据: " + "；".join(r["reasons"]))

    if alert_texts or thunder:
        da = disaster_advice(thunder, alert_texts)
        if da:
            lines.append("【灾害防御】\n" + "\n".join(da))

    # 紫外线 / 风力 防护建议
    uv_val = uv.get("value") if isinstance(uv, dict) else None
    ua = uv_advice(uv_val, (uv.get("category") if isinstance(uv, dict) else None))
    if ua:
        lines.append("【防晒提醒】\n" + "\n".join(ua))
    wscale = qw.get("scale") if isinstance(qw, dict) else None
    wa = wind_advice(wscale, None)
    if wa:
        lines.append("【防风提醒】\n" + "\n".join(wa))

    # 空气污染提醒
    if isinstance(aq, dict) and aq.get("aqi") is not None:
        aa = aqi_advice(aq.get("aqi"), aq.get("level"))
        if aa:
            lines.append("【空气提醒】\n" + "\n".join(aa))

    lines.append("结论: " + ("可以出门运动" if go else "建议跳过户外运动 / 改室内"))

    title = f"天气·{'可出行' if go else '不建议'} {r['time']}"
    desp = "\n\n".join(lines)
    _log(title)
    _log(desp)

    # 统一推送：优先群机器人(云端)，回退自建应用(需可信IP)
    wc_content = f"## {title}\n{desp}"
    resp = push_all(wc_content)
    if resp is None:
        print("推送失败：请检查 WECOM_WEBHOOK_URL（云端主通道）或 "
              "WECOM_CORPID/APP_SECRET/AGENTID/TOUSER（自建应用，需配置企业可信IP）")


if __name__ == "__main__":
    main()
