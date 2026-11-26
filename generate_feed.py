# -*- coding: utf-8 -*-
"""
自动生成 StickyDesk 的推广数据.json（广告 feed），对接蚂蚁星球开放平台。

背景
----
便签广告位读的是公开 JSON：Ms67/desk-ad 仓库根目录的「推广数据.json」。
以前靠人工找商品、填链接、写文案；本脚本把这个流程自动化：

  1. 调蚂蚁星球 pdd/goodslist 拉「高佣 + 有券」的拼多多商品；
  2. 调 pdd/getunionurl 把每个商品转成带推广位的 p.pinduoduo.com 短链；
  3. 套简短文案模板，生成和现有格式一模一样的 JSON；
  4. 原子写入「推广数据.json」。GitHub Actions 每天定时跑并 push。

客户端 ad_feed.py 每 5 分钟拉一次这个 JSON，因此**改 feed 不用改客户端、
不用重新打包、不用用户更新软件**——这就是「不用自己维护广告链接」的关键。

配置（全部走环境变量，密钥不落盘、不进日志）
------------------------------------------------
  CPS_APIKEY     必填，蚂蚁星球 apikey（https://www.haojingke.com 免费申请）
  CPS_PID        可选，拼多多推广位 pid，不填用蚂蚁星球后台的默认推广位
  FEED_FILE      可选，输出文件路径，默认脚本同目录的「推广数据.json」
  FEED_COUNT     可选，生成条数，默认 8（标题栏轮播，8 条足够）
  MIN_COMMISSION 可选，最低佣金比例(%)，默认 5

用法
----
  python generate_feed.py            # 正常生成（需 CPS_APIKEY）
  python generate_feed.py --selftest # 自测：用假数据跑通全流程，不联网、不写文件

设计原则（防崩溃 / 不产生脏数据）
----------------------------------
  · 任何一步失败都不覆盖旧 feed：先全部生成好、校验通过，最后才原子替换；
  · 有效条目不足 MIN_ITEMS 时放弃本次，保留上一版，绝不把广告清空；
  · 转链失败的单个商品跳过，不影响其它条目；
  · 文案模板固定、内容来自接口，不读任何本地隐私数据。
"""

import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

API_BASE = "http://api-gw.haojingke.com/index.php/v1/api/pdd"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StickyDesk-feed-bot/1.0"

# 标题栏广告文案模板：短、口语、带 emoji，和现有 feed 风格一致。
# {kw} 会替换成商品关键词（截取商品名的一段）。
TITLE_TPL = (
    "今日好价 {kw}😊",
    "这个真香 {kw}😋",
    "闭眼入 {kw}✨",
    "捡到宝了 {kw}😮",
    "划算 {kw}😄",
    "种草 {kw}👍",
    "手慢无 {kw}🔥",
    "强烈安利 {kw}💖",
    "省心好物 {kw}🌟",
    "超值推荐 {kw}🎉",
    "别错过 {kw}🙌",
    "爆款 {kw}⚡",
)

MIN_ITEMS = 4          # 少于这个数就放弃本次，保留旧 feed
TIMEOUT = 20


def log(msg):
    print("[feed] " + str(msg), flush=True)


def api_get(path, params, timeout=TIMEOUT):
    """GET 一个蚂蚁星球接口，返回解析后的 dict；失败返回 None。"""
    url = API_BASE + path + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        log("请求失败 %s：%s" % (path.split("/")[-1], e))
        return None


def fetch_goods(apikey, count, min_commission):
    """拉高佣、有券的拼多多商品列表，返回商品 dict 列表。"""
    params = {
        "apikey": apikey,
        "page": 1,
        "page_size": max(count * 3, 20),   # 多拉一些，转链失败时有备选
        "sort_type": 2,                    # 按佣金比例降序
        "with_coupon": 1,                  # 只要有券商品
    }
    if min_commission > 0:
        params["mincommissionshare"] = min_commission
    res = api_get("/goodslist", params)
    if not res or res.get("status_code") != 200:
        log("商品列表接口返回异常：%s" % (res.get("message") if res else "无响应"))
        return []
    goods = (((res.get("data") or {}).get("goods_list"))
             or ((res.get("data") or {}).get("list")) or [])
    if not isinstance(goods, list):
        return []
    log("拉到 %d 个候选商品" % len(goods))
    return goods


def convert_link(apikey, goods_sign, goods_id, pid=""):
    """转链：返回 p.pinduoduo.com 推广短链；失败返回空串。

    2021-03-15 后拼多多要求用 goods_sign，goods_id 作为兜底。
    """
    params = {"apikey": apikey}
    if goods_sign:
        params["goods_sign"] = goods_sign
    elif goods_id:
        params["goods_id"] = goods_id
    else:
        return ""
    if pid:
        params["pid"] = pid
    res = api_get("/getunionurl", params)
    if not res or res.get("status_code") != 200:
        return ""
    data = res.get("data")
    if isinstance(data, dict):
        link = data.get("data") or (data.get("alldata") or {}).get("short_url") or ""
        return str(link).strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def keyword_of(name, limit=8):
    """从商品名里截一段做关键词，去掉常见噪音词。"""
    name = (name or "").strip()
    for noise in ("买", "正品", "官方", "旗舰店", "批发", "家用"):
        name = name.replace(noise, "")
    name = name.strip()
    return name[:limit] if name else "好物"


def detail_of(item):
    """详情：商品名 + 券后价，悬停滚动显示。"""
    name = str(item.get("goods_name") or item.get("goods_short_name") or "").strip()
    price = item.get("price_after") or item.get("price_pg") or item.get("price")
    bits = [name] if name else []
    try:
        if price and float(price) > 0:
            bits.append("券后 ¥%.2f" % float(price))
    except (ValueError, TypeError):
        pass
    return " · ".join(bits) if bits else "精选好物"


def build_feed(goods, apikey, pid, count):
    """把商品列表转成 feed 数组；返回 (条目列表, 成功数)。"""
    items = []
    seen = set()
    for idx, g in enumerate(goods):
        if len(items) >= count:
            break
        link = convert_link(apikey,
                            str(g.get("goods_sign") or "").strip(),
                            str(g.get("goods_id") or "").strip(),
                            pid)
        if not link or not link.lower().startswith(("http://", "https://")):
            continue
        if link in seen:
            continue
        seen.add(link)
        title = TITLE_TPL[idx % len(TITLE_TPL)].format(
            kw=keyword_of(g.get("goods_short_name") or g.get("goods_name")))
        items.append({
            "序号": len(items) + 1,
            "推广文案": title,
            "推广链接": link,
            "详情": detail_of(g),
            "备注": "",
            "更新日期": time.strftime("%Y/%m/%d"),
        })
        # 礼貌间隔，避免转链接口限流
        time.sleep(0.2)
    return items, len(items)


def write_atomic(path, items):
    """原子写入：先写临时文件，成功后替换，避免写到一半被客户端读到。"""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(folder, ".feed.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run(feed_file, apikey, pid, count, min_commission):
    goods = fetch_goods(apikey, count, min_commission)
    if not goods:
        log("没有拿到商品，保留旧 feed")
        return False
    items, n = build_feed(goods, apikey, pid, count)
    if n < MIN_ITEMS:
        log("有效条目只有 %d 条（<%d），放弃本次，保留旧 feed" % (n, MIN_ITEMS))
        return False
    write_atomic(feed_file, items)
    log("已写入 %d 条到 %s" % (n, feed_file))
    return True


def selftest():
    """自测：假数据跑通转换 + 原子写入 + 客户端格式兼容，不联网。"""
    global API_BASE
    fake_goods = [
        {"goods_id": 1, "goods_sign": "s1", "goods_name": "花香护手霜套装补水保湿",
         "goods_short_name": "花香护手霜套装", "price_after": 19.9},
        {"goods_id": 2, "goods_sign": "s2", "goods_name": "鸭舌卤味零食80g",
         "goods_short_name": "鸭舌卤味", "price_after": 29.9},
        {"goods_id": 3, "goods_sign": "s3", "goods_name": "扫地机器人滚筒洗地",
         "goods_short_name": "扫地机器人", "price_after": 999},
        {"goods_id": 4, "goods_sign": "s4", "goods_name": "男士工装夹克",
         "goods_short_name": "男士工装夹克", "price_after": 89},
    ]
    # 用假的 convert_link，避免联网
    real_convert = convert_link
    globals()["convert_link"] = lambda key, sign, gid, pid="": (
        "https://p.pinduoduo.com/test%s" % (sign or gid))
    try:
        items, n = build_feed(fake_goods, "fake", "", 8)
        assert n == 4, "应生成 4 条，实际 %d" % n
        keys = {"序号", "推广文案", "推广链接", "详情", "备注", "更新日期"}
        for it in items:
            assert set(it.keys()) == keys, "字段不匹配：%s" % it.keys()
            assert it["推广链接"].startswith("https://"), it
            assert it["推广文案"] and it["详情"], it
        # 验证原子写入 + 能被客户端 parse_feed 解析
        tmpfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest_feed.json")
        write_atomic(tmpfile, items)
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import ad_feed
        parsed = ad_feed.parse_feed(open(tmpfile, encoding="utf-8").read())
        assert len(parsed) == 4, "客户端应解析出 4 条，实际 %d" % len(parsed)
        os.remove(tmpfile)
        log("自测通过：4 条 feed，字段/链接/客户端解析全部 OK")
        return True
    finally:
        globals()["convert_link"] = real_convert


def main():
    if "--selftest" in sys.argv:
        return 0 if selftest() else 1
    apikey = (os.environ.get("CPS_APIKEY") or "").strip()
    if not apikey:
        log("缺少 CPS_APIKEY 环境变量（蚂蚁星球 apikey），放弃")
        return 2
    pid = (os.environ.get("CPS_PID") or "").strip()
    here = os.path.dirname(os.path.abspath(__file__))
    feed_file = os.environ.get("FEED_FILE") or os.path.join(here, "推广数据.json")
    try:
        count = max(MIN_ITEMS, min(int(os.environ.get("FEED_COUNT", "8")), 50))
        min_commission = max(0, int(os.environ.get("MIN_COMMISSION", "5")))
    except ValueError:
        count, min_commission = 8, 5
    ok = run(feed_file, apikey, pid, count, min_commission)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
