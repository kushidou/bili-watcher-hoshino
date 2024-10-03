import json
import time
import datetime
import difflib
import httpx
import asyncio
import configparser as cfg
import os
from os.path import dirname, join, exists, getmtime
from .res import drawCard
from .res import wbi
from .res import auth
from loguru import logger as log

help_info = """=== bili-notice-hoshino 帮助 ===
    
bili-ctl para1 para2 para3 [...]
关键词过滤  black-words  uid  add/remove 拼多多 pdd ... 
查看关键词  black-words  uid  list  
开奖动态   islucky  uid  true/false
重新加载    reload
昵称控制    add-nick/del-nick   uid  短昵称
昵称查询    list-lick   uid
帮助菜单   help
*功能性指令只能由机器人管理员操作*"""

# 路径配置
curpath = dirname(__file__)
watcher_file = join(curpath, "upperlist.json")
res_dir = join(curpath, "res/")
up_dir = join(curpath, "uppers/")

# 全局变量
number = 0  # 轮询的编号
up_latest = {}  # 各个up主及其动态记录
live_latest = {}  # 各个up主直播状态记录
up_list = []  # up主列表
cache_clean_date = 0
number_live = 0  # 直播轮询的编号
flag_number_live = 5  # 默认每轮询5次，检查是否有主播开播。
gcookies = None
cookies_fail = 0

p = {"all://": None}


def up_history_write(uid: str, skin=None):
    global up_latest, up_dir, live_latest

    if skin is None and exists(up_dir + uid + ".json"):
        with open(up_dir + uid + ".json", "r", encoding="UTF-8") as f:
            j = json.load(f)
        if "skin" in j:
            skin = j["skin"]
    else:
        skin = {}
    with open(up_dir + uid + ".json", "w", encoding="UTF-8") as f:  # 更新记录文件
        json.dump(
            {"history": up_latest[uid], "live": live_latest[uid], "skin": skin},
            f,
            ensure_ascii=False,
        )


# 读取配置文件
if not exists(join(curpath, "config.ini")):
    try:
        os.rename(join(curpath, "config_example.ini"), join(curpath, "config.ini"))
    except Exception as e:
        print(
            f"\r\n\033[1;41m[Error]\033[0m\tBili-notice:\tCannot Find config.ini or config_example.ini !!! \r\n{e}"
        )
conf = cfg.ConfigParser(allow_no_value=True)
conf.read(join(curpath, "config.ini"), encoding="utf-8")
comcfg = conf.items("common")
drawcfg = conf.items("drawCard")

if conf.has_option("common", "pool_live"):
    flag_number_live = int(conf.get("common", "pool_live"))
if conf.getboolean("common", "only_video"):
    available_type = [8]
elif conf.getboolean("common", "only_dynamic"):
    available_type = [2, 4]
else:
    available_type = [
        2,  # Picture
        4,  # text
        8,  # video
        64,  # article
        256,  # audio
        512,  # bangumi
        2048,  # H5event
    ]

if conf.has_option("common", "allow_follow_illegal"):
    allow_follow_illegal = conf.getboolean("common", "allow_follow_illegal")
else:
    allow_follow_illegal = False

log_level = conf.get("common", "log_level").upper()
if log_level not in ["ERROR", "WARN", "INFO", "DEBUG", "TRACE"]:
    print(f"Config Error: log_level(={log_level}) not correct! Force log_level to INFO")
    log_level = "INFO"
log_max_days = conf.get("common", "log_max_days")
if not log_max_days.isdigit():
    log_max_days = 15
    print(f"Config Error: log_max_days get ({log_max_days}), we need number!")

# 初始化日志系统
path_log = join(dirname(__file__), "log/")
if not exists(path_log):
    os.mkdir(path_log)
log.add(
    path_log + "{time:YYYY-MM-DD}.log",
    level=log_level,
    rotation="04:00",
    retention=f"{log_max_days} days",
    backtrace=False,
    enqueue=True,
    diagnose=False,
    format="{time:HH:mm:ss} [{level}] \t{message}",
)


# 从文件中读取up主配置列表和up主发送动态的历史
up_group_info, up_list = {}, []
if exists(f"{up_dir}list.json"):
    with open(join(up_dir, "list.json"), "r", encoding="UTF-8") as f:
        up_group_info = json.load(f)

    up_list = list(up_group_info.keys())
    for uid in up_list:
        if exists(up_dir + uid + ".json"):
            with open(up_dir + uid + ".json", "r", encoding="UTF-8") as f:
                j = json.load(f)

            up_latest[uid] = j["history"]

            if "live" in j:
                live_latest[uid] = j["live"]
            else:
                live_latest[uid] = 0
                up_history_write(uid)

        else:
            up_latest[uid] = []
            live_latest[uid] = 0
            up_history_write(uid)

log.info(f"Load up list success: {len(up_list)}")

# 组成昵称查找
gw_user = {}
gw_nick = {}

for id in up_group_info:
    u = up_group_info[id]
    if u.get("nick"):
        gw_user[u["uname"]] = {"uid": u["uid"], "nick": u["nick"]}
        for n in u["nick"]:
            gw_nick[n] = {"uname": u["uname"], "uid": u["uid"]}
    else:
        gw_user[u["uname"]] = {"uid": u["uid"], "nick": []}
gw_name_list = gw_user.keys()
gw_nick_list = gw_nick.keys()


async def get_update():
    global number, up_latest, up_list, cache_clean_date, up_group_info, number_live, flag_number_live, cookies_fail
    dyimg, dytype = None, None
    rst, suc, fai = 0, 0, 0
    dynamic_list = []
    header = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11; Redmi K30 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.54 Mobile Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    if number >= len(up_list):  # 序号异常大，清除
        number = 0

    if len(up_group_info) == 0:
        return 0, []

    # 借用轮询来清理垃圾和检查更新
    cache_clean_today = datetime.date.today().day
    if cache_clean_today != cache_clean_date:
        clean_cache()
        await check_plugin_update()
        cache_clean_date = cache_clean_today
    # 尝试更新cookies
    gcookies = await auth.update_cookies(cookies_fail)
    cookies_fail = 0

    # 提取下一个up，如果没有人关注的话，状态改成false，跳过不关注的人
    maxcount = len(up_list)

    # 每flag_number_live次轮询，查一次直播间信息
    number_live += 1
    if flag_number_live <= number_live:
        print("Check live rooms.")
        number_live = 0
        liverst, livelist = await live_check()
        # 由于发现了新的api可以一次性查询所有直播间，与之前的设计不同，直播功能无法很好的结合进原有代码中。
        # 所以，这里采用直接return的方法，避免产生其他纠葛
        return liverst, livelist

    while 1:
        if up_list[number] not in up_group_info.keys():
            number = 0 if number + 1 >= len(up_list) else number + 1
            continue
        this_up = up_group_info[up_list[number]]
        if this_up["watch"] is True:  # 跳过不监控的up
            if len(this_up["group"]) != 0:
                break  # up主状态正常，跳出循环
            up_group_info[up_list[number]]["watch"] = False
            with open(join(up_dir, "list.json"), "w", encoding="UTF-8") as f:
                json.dump(up_group_info, f, ensure_ascii=False)
        else:
            if maxcount <= 0:  # 避免死循环不跳出
                return
            else:
                maxcount = maxcount - 1
            number = 0 if number + 1 >= len(up_list) else number + 1
    if this_up["watch"]:
        uid_str = up_list[number]
        print(f"Check list of {uid_str}")
        # print(f'[Debug] Start getting ID={uid_str}')
        try:
            async with httpx.AsyncClient(proxies=p) as client:
                res = await client.get(
                    url=f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={uid_str}",
                    headers=header,
                    cookies=gcookies,
                )
        except Exception as e:
            log.info(f"Err: Get dynamic list failed.\n{e}")
            number = 0 if number + 1 >= len(up_list) else number + 1
            return -1, []
        if res.status_code != 200:
            log.warning(f"get_update() fail: Server status code = {res.status_code}")
        res.encoding = "utf-8"  # 兼容python3.9
        # print(f'收到的结果长度有{len(res.text)}, 其中 code=', end='' )
        dylist = json.loads(res.text)
        if dylist["code"] != 0:
            log.warning(
                f'dynamic list get fail: Server OK but code={dylist["code"]}, 您可能被风控. '
            )
            number = 0 if number + 1 >= len(up_list) else number + 1
            return -1, []
        # 可能返回空内容

        if len(dylist["data"]["items"]) == 0:
            log.warning("获取到的动态数量为0, 您可能被风控.")
            cookies_fail = 1
            number = 0 if number + 1 >= len(up_list) else number + 1
            return -1, []

        for card in dylist["data"]["items"]:
            # api更改，动态卡片id变化
            if int(card["id_str"]) in up_latest[up_list[number]]:
                break

            # 发布时间判断逻辑提前
            pub_time = card["modules"]["module_author"]["pub_ts"]
            if conf.getint("common", "available_time") * 60 < (
                int(time.time()) - pub_time
            ):
                log.info(
                    f"This dynamic({card['id_str']}) is too old: {m2hm(time.time() - pub_time)} minutes ago\n"
                )
                up_latest[uid_str].append(
                    int(card["id_str"])
                )  # (无论成功失败)完成后把动态加入肯德基豪华午餐
                up_history_write(uid_str)  # 更新记录文件
                fai -= 1
                continue

            # 获取动态信息，这个是旧版API格式，可以不用大幅修改解析代码 / 2024.07.14
            async with httpx.AsyncClient(proxies=p) as client:
                res = await client.get(
                    url=f'https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/get_dynamic_detail?dynamic_id={card["id_str"]}',
                    headers=header,
                    cookies=gcookies,
                )
            if res.status_code != 200:
                log.warning(
                    f"Dynamic detail API return = {res.status_code}, 您可能被风控"
                )
                cookies_fail = 1
                fai -= 1
                continue
            old_card = json.loads(res.text)

            if old_card["code"] != 0:
                log.warning(
                    f'dynamic detail info get fail: Server OK but code={dylist["code"]}, 您可能被风控. '
                )
                cookies_fail = 1
                fai -= 1
                continue

            if len(res.text) < 100:
                log.warning(
                    f"动态详细信息太短, 只有{len(res.text)}字节, 您可能被风控. "
                )
                cookies_fail = 1
                fai -= 1
                continue

            # 解析动态json, 由于API变化需要重写
            dynamic = drawCard.Card(old_card["data"]["card"])

            if not dynamic.json_decode_result:
                log.error(
                    f'动态内容解析失败，id={card["desc"]["dynamic_id_str"]}, 详见drawCard日志。'
                )
                up_latest[uid_str].append(dynamic.dyid)
                continue

            # 更新UP主的昵称
            if dynamic.nickname != this_up["uname"]:
                log.info(
                    f'更新UP主名称:  uid={this_up["uid"]}, nickname [{this_up["uname"]}] ==> [{dynamic.nickname}]'
                )
                up_group_info[up_list[number]]["uname"] = dynamic.nickname
                with open(join(up_dir, "list.json"), "w", encoding="UTF-8") as f:
                    json.dump(up_group_info, f, ensure_ascii=False)

            log.info("========== New Dynamic Card =========")
            log.info(
                f"UP={dynamic.nickname}({dynamic.uid}), Dynamic_id={dynamic.dyid}, Type={int(dynamic.dytype)}, ori_type={int(dynamic.dyorigtype)}"
            )
            if (not conf.getboolean("common", "repost")) and dynamic.dytype == 1:
                log.info("已设置不分享转发类动态。\n")
                fai -= 1
                continue
            try:
                if dynamic.dytype == 64:
                    log.debug("动态类型64, 检查问题所在:")
                if not dynamic.check_black_words(
                    conf.get("common", "global_black_words"),
                    this_up["ad_keys"],
                    this_up["islucky"],
                ):  # 如果触发过滤关键词，则忽视该动态
                    if dynamic.dytype == 64:
                        log.debug("动态类型64, 通过了黑名单词汇检查")
                    if dynamic.is_realtime(
                        conf.getint("common", "available_time")
                    ):  # 太久的动态不予发送
                        # 只解析支持的类型

                        if dynamic.dytype in available_type or (
                            dynamic.dytype == 1 and dynamic.dyorigtype in available_type
                        ):
                            drawBox = drawCard.Box(conf)  # 创建卡片图片的对象
                            dyimg, dytype = await dynamic.draw(
                                drawBox, conf.getboolean("cache", "dycard_cache")
                            )  # 绘制动态
                            # msg = f"{dynamic.nickname} {dytype}, 点击链接直达：\n https://t.bilibili.com/{dynamic.dyidstr}  \n[CQ:image,file={dyimg}]"
                            dyinfo = {
                                "nickname": dynamic.nickname,
                                "uid": dynamic.dyid,
                                "type": dytype,
                                "subtype": dynamic.dyorigtype,
                                "time": dynamic.dytime,  # 时间戳，非字符串时间
                                "pic": dyimg,
                                "link": f"https://t.bilibili.com/{dynamic.dyidstr}",
                                "sublink": "",
                                "group": this_up["group"],
                            }
                            if dynamic.dytype == 64:
                                log.debug("动态类型64, 绘制并组织完成")

                            dynamic_list.append(dyinfo)
                            suc += 1
                        else:
                            log.info(
                                f"(type={dynamic.dytype}, subtype={dynamic.dyorigtype}) 未受支持! 🕊🕊🕊 或者设置为不发送\n"
                            )
                    else:
                        log.info(
                            f"This dynamic({dynamic.dyid}) is too old: {m2hm(time.time() - dynamic.dytime)} minutes ago\n"
                        )
                        fai -= 1
                else:
                    log.info(f"({dynamic.dyid})触发过滤词，或者是转发抽奖动态。\n")
                    fai -= 1
            except Exception as e:
                if dynamic.dytype == 64:
                    log.debug("动态类型64, 触发了try-exception")
                log.warning(e)
            finally:
                up_latest[uid_str].append(
                    dynamic.dyid
                )  # (无论成功失败)完成后把动态加入肯德基豪华午餐
                up_history_write(uid_str, dynamic.getskin())  # 更新记录文件
                log.debug("Write skin info into up history\n")

    rst = fai if suc == 0 else suc
    number = 0 if number + 1 >= len(up_list) else number + 1
    return rst, dynamic_list


async def follow(uid, group):  # sync to async
    global number, up_latest, up_list, gcookies
    """关注UP主,并创建和修改对应的记录文件

    Args:
        uid (num): up主的uuid,仅接受通过uuid来关注
        gruop (num): 申请的群

    Returns:
        rst (bool): 申请的结果。
        msg (str):  结果的原因。成功后是  昵称[id]
    """
    if not uid.isdigit():
        msg = "请输入正确的UID!"
        log.info(f"关注失败,UID错误: {uid}")
        return False, msg

    if uid not in up_list:  # 从未添加过
        para = {"mid": str(uid)}
        retry_time = 3
        header = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Connection": "close",
            "Host": "api.bilibili.com",
            # 'Upgrade-Insecure-Requests': '1',
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36 Edg/112.0.1722.58",
        }

        while True:
            # wbi认证尝试三次失败，放弃继续尝试并增加无名氏
            if retry_time <= 0:
                msg = "【联网核查失败】"
                return follow_illegal(uid, group, msg)
            # 从服务器获取信息
            if gcookies is None:
                gcookies = await auth.update_cookies()
            try:
                if gcookies is None:
                    print("小饼干不存在")

                async with httpx.AsyncClient(proxies=p) as client:
                    res = await client.get(
                        url="https://api.bilibili.com/x/space/wbi/acc/info",
                        params=wbi.encode(para),
                        headers=header,
                        cookies=gcookies,
                    )
            except Exception as e:
                msg = "网络出错了，请稍后再试~"
                log.info(f"关注失败，网络错误\n{e}")
                return False, msg
            # 应付阿b返回两段json的情况
            if "}{" in res.text:
                jtext = "{" + res.text.split("}{")[1]
                print("阿B返回了两段json, 取第二段")
            else:
                jtext = res.text
            # 解析json字串，防止返回的东西不对
            try:
                resj = json.loads(jtext)
            except Exception:
                resj = {"code": 100, "data": {"name": "~unknow~"}}
            # 根据错误码情况处理
            if resj["code"] != 200:
                # 成功
                if resj["code"] == 0:
                    upinfo = {
                        "uid": int(uid),
                        "uname": resj["data"]["name"],
                        "group": [group],
                        "watch": True,
                        "islucky": True,
                        "ad_keys": [],
                        "live": True,
                    }
                    up_group_info[uid] = upinfo
                    try:
                        with open(
                            join(up_dir, "list.json"), "w", encoding="UTF-8"
                        ) as f:  # 更新UP主列表
                            json.dump(up_group_info, f, ensure_ascii=False)
                        up_latest[uid] = []
                        live_latest[uid] = 0
                        up_history_write(uid)
                        print(
                            f'add {upinfo["uname"]}({uid}) history json to {up_dir+uid}.json'
                        )
                        up_list = list(up_group_info.keys())
                    except Exception as e:
                        msg = "UP主数据写入失败，请查看日志。"
                        log.info(
                            f"关注失败,无法修改list文件或无法创建用户记录文件，详细信息为:{e}"
                        )
                        return False, msg

                    msg = f'{upinfo["uname"]}[{uid}]'
                    log.info(
                        f'关注成功，群: {group}，用户: {up_group_info[uid]["uname"]}({uid})'
                    )
                    return True, msg

                elif resj["code"] == -403:
                    await wbi.update()
                    log.info(
                        f'API返回"-403 访问权限不足"，即将更新密钥然后尝试：{abs(4-retry_time)}'
                    )
                    retry_time -= 1
                    # time.sleep(3)
                    await asyncio.sleep(3)

                else:
                    log.info(
                        f'API返回(code={resj["code"]}, message={resj["message"]})，即将重试：{abs(4-retry_time)}'
                    )
                    retry_time -= 1
                    # time.sleep(3)
                    await asyncio.sleep(3)
            else:
                # 服务器返回200，查无此人
                msg = "UID有误。"
                log.info(f"关注失败，查无此人(uid={uid})")
                return False, msg

    elif group in up_group_info[uid]["group"]:
        log.info("关注失败，已经关注过了")
        return False, "已经关注过惹~"
    else:
        up_group_info[uid]["group"].append(group)
        up_group_info[uid]["watch"] = True
        try:
            with open(join(up_dir, "list.json"), "w", encoding="UTF-8") as f:
                json.dump(up_group_info, f, ensure_ascii=False)
        except Exception:
            log.info("关注失败,无法修改list文件或无法创建用户记录文件")
            return False, "UP主文件写入失败，未知错误，请检查数据文件。"
        msg = f'{up_group_info[uid]["uname"]}[{uid}]'
        return True, msg


def follow_illegal(uid, group, rawmsg):
    global number, up_latest, up_dir, up_list, gcookies, allow_follow_illegal
    if allow_follow_illegal:
        upinfo = {
            "uid": int(uid),
            "uname": "null",
            "group": [group],
            "watch": True,
            "islucky": True,
            "ad_keys": [],
            "live": True,
        }
        up_group_info[uid] = upinfo
        try:
            with open(
                join(up_dir, "list.json"), "w", encoding="UTF-8"
            ) as f:  # 更新UP主列表
                json.dump(up_group_info, f, ensure_ascii=False)

            up_latest[uid] = []
            live_latest[uid] = 0
            up_history_write(uid)
            up_list = list(up_group_info.keys())

        except Exception as e:
            msg = "UP主数据写入失败，请查看日志。"
            log.info(f"关注失败,无法修改list文件或无法创建用户记录文件，详细信息为:{e}")
            return False, msg
        log.info("关注流程失败,按用户配置开始记录uid")
        return True, f"{rawmsg},【记录uid】"
    else:
        return False, rawmsg


def unfollow(uid, group):
    global number, up_latest, up_list
    """取关UP主，并更新有关文件

    Args:
        uid (num): 被取关的UP主ID
        group (num): 申请取关的群

    Returns:
        bool: 执行结果。
        str:  结果信息。
    """
    rst = False
    msg = "未知错误。"
    if not uid.isdigit():
        msg = "请输入正确的UID!"
        log.info(f'取关失败，UID错误: "{uid}"')
    elif uid in up_list:
        if group not in up_group_info[uid]["group"]:
            msg = "没有关注ta哦~"
            log.info(f"取关失败，该群({group})未关注用户({uid})")
            log.debug(f'用户{uid} 被关注的群包含{up_group_info[uid]["group"]}')
        else:
            try:
                up_group_info[uid]["group"].remove(group)
                with open(join(up_dir, "list.json"), "w", encoding="UTF-8") as f:
                    json.dump(up_group_info, f, ensure_ascii=False)
                # del up_latest[uid]    # 出错，取关导致up主被动态历史的列表清除，实际上不关注的人也会进这个列表
            except Exception:
                log.info("取关失败,无法修改list文件")
                return False, "UP主文件修改失败，未知错误，请手动检查配置文件。"
            msg = f'已经取关{up_group_info[uid]["uname"]}({uid})惹~'
            rst = True
            log.info(
                f'取关成功，群: {group}，用户: {up_group_info[uid]["uname"]}({uid})'
            )
    else:
        msg = "没有关注ta哦~"
        log.info(f"取关失败，该用户({uid})从未添加。")
    return rst, msg


# 直播间检查，找开播的人
async def live_check():  # sync to async
    global up_group_info, up_list, live_latest, up_dir, conf, number_live, flag_number_live
    rst = -1000
    dylist = []

    url = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
    header = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "user-agent": "curl/7.0.0",
    }
    data = json.dumps({"uids": up_list})
    async with httpx.AsyncClient(proxies=p) as client:
        res = await client.post(url=url, data=data, headers=header)
        # res = await client.get(url=url+'?'+para, headers=header)
    # res = requests.post(url=url, data=data, headers=header)
    if res.status_code != 200:
        log.warning(f"直播间查询失败，服务器返回{res.status_code}")
        return 0, []
    result = json.loads(res.text)
    if result["code"] != 0:
        log.warning(f'直播间查询失败，错误为{result["msg"]}')
        return 0, []
    rooms = result["data"]

    for uid in rooms.keys():
        room = rooms[uid]
        status = room["live_status"]
        if live_latest[uid] == status or abs(live_latest[uid] - status) == 2:
            # 状态不变时直接跳过。如果从停播切换到轮播，也跳过。
            continue

        if status == 1:  # 0未开播，1直播，2轮播
            # 开始直播
            # 检查是否发布通知，默认都发布
            thisup = up_group_info[uid]
            if "live" in thisup and not thisup["live"]:
                continue
            if not thisup["watch"]:
                # 未被关注，跳过
                continue
            log.info(
                f'[LIVE] {thisup["uname"]}({uid}) 开始直播。 state= {live_latest[uid]} --> {status}'
            )
            live_latest[uid] = 1
            up_history_write(uid)
            with open(up_dir + uid + ".json", "r", encoding="UTF-8") as f:
                j = json.load(f)
            skin = j["skin"] if "skin" in j else None
            # 完全处理json
            live = drawCard.Live(room)
            drawBox = drawCard.Box(conf)  # 创建卡片图片的对象
            roomimg, roomtype = await live.draw(drawBox, skin, False)

            roominfo = {
                "nickname": live.nickname,
                "uid": live.uid,
                "type": roomtype,
                "subtype": "",
                "time": time.time(),  # 开播为即时消息，时间戳不重要
                "pic": roomimg,
                "link": f"https://live.bilibili.com/{live.roomid}",  # https://live.bilibili.com/528
                "sublink": "",
                "group": thisup["group"],
            }
            dylist.append(roominfo)
            rst -= 1

        elif status in [0, 2]:
            log.info(
                f'[LIVE] {up_group_info[uid]["uname"]}({uid}) 下播了。 state= {live_latest[uid]} --> {status}'
            )
            live_latest[uid] = status
            up_history_write(uid)

    return rst, dylist


async def shell(group, para, right):
    """类指令的热管理工具

    Args:
        group (num): 发起设置的群号
        para (str): 完整指令
        right (bool): 权限判断。
    """
    global up_group_info, up_list
    rst = True
    msg = '指令有误，请检查! "bili-ctl help" 可以查看更多信息'
    try:
        cmd = para[0]
    except Exception:
        cmd = "help"

    log.info(f"--指令控制--  功能:{cmd}, 参数:{para[1:]}, 权限:{right}")

    if cmd == "black-words":
        rst, msg = await cmd_blklist(group, para, right)
    elif cmd == "islucky":
        rst, msg = await cmd_islucky(group, para, right)
    elif cmd.upper() == "RELOAD":
        if not right:
            return False, "你没有权限这么做"
        with open(join(up_dir, "list.json"), "r", encoding="UTF-8") as f:
            up_group_info = json.load(f)
            up_list = list(up_group_info.keys())
        msg = "信息更新完成!"
    elif cmd == "add-nick":
        rst, msg = await cmd_nick(group, para, right, "add")
    elif cmd == "del-nick":
        rst, msg = await cmd_nick(group, para, right, "del")
    elif cmd in {"list-nick", "ls-nick"}:
        rst, msg = await cmd_nick(group, para, True, "list")

    elif cmd == "help":
        msg = help_info

    msg = msg.replace("'", "")
    msg = msg.replace("[", "")
    msg = msg.replace("]", "")
    print(f"bili-ctl return msg: {msg}")
    return rst, msg


def get_follow(group: int, level: int = 0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (int):    查询的群号
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    count = 0
    txt = "本群已关注：\r\n"
    for uid in up_group_info.keys():
        if group in up_group_info[uid]["group"]:
            txt += f'{up_group_info[uid]["uname"]}({uid})'
            if level >= 2:
                txt += f'\r\n  是否过滤转发抽奖: {up_group_info[uid]["islucky"]}'
                txt += f'\r\n  过滤关键词有: {str(up_group_info[uid]["ad_keys"])}'
            if level >= 9:
                txt += f'\r\n  关注的群号有: {str(up_group_info[uid]["group"])}'
            txt += "\r\n"
            count += 1

    rst = bool(count)
    info = f"{txt}共{count}位UP主" if count else "本群未关注任何UP主！"
    return rst, info


def get_follow_byuid(group: str, level: int = 0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (str):    str输入all，将会显示所有的up主，包含watch=false的
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    if group != "all":
        return False, "函数参数错误，仅接受'all'"
    count = 0
    txt = "本bot已关注：\r\n"
    for uid in up_group_info.keys():
        txt += f'{up_group_info[uid]["uname"]}({uid})'
        if level >= 9:
            txt += f'\r\n  是否过滤转发抽奖: {up_group_info[uid]["islucky"]}'
            txt += f'\r\n  过滤关键词有: {str(up_group_info[uid]["ad_keys"])}'
        if level >= 2:
            txt += f'\r\n  群号: {str(up_group_info[uid]["group"])}'
        txt += "\r\n"
        count += 1
    rst = bool(count)
    info = f"{txt}共{count}位UP主" if count else "您还没有关注任何UP主。"
    return rst, info


def get_follow_bygrp(group: str, level: int = 0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (str):    str输入all，将会显示所有的up主，包含watch=false的
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    count = 0
    txt = "群关注列表汇总：\r\n"
    lists = {}
    # 遍历up主，把uid分类到群信息
    for uid in up_group_info.keys():
        for grp in up_group_info[uid]["group"]:
            if grp in lists:
                lists[grp].append(uid)
            else:
                lists[grp] = [uid]
        count += 1
    # 按群生成文字消息
    for g in lists:
        txt += f"群{g}已关注:\r\n"
        for u in lists[g]:
            txt += f'  {up_group_info[str(u)]["uname"]}({u})\r\n'
        txt += "\r\n"

    rst = bool(count)
    info = txt[:-2] if count else "还没有关注任何UP主。"
    return rst, info


async def guess_who(keywds: str):
    """利用搜索功能，猜测昵称指代的用户
        该功能效率和成功率都低，谨慎使用。
        每个用户增加昵称的配置项，匹配时优先全匹配gw_nick_list，然后模糊匹配gw_name_list，
        最后利用b站的搜索API进行搜寻，返回第一个结果。
        匹配结束后，不会保存，请调用另一个接口

    Args:
        keywds (str): 关键词

    Returns:
        uid (int):      查询的uid结果，匹配失败=0
        uname (str):    查询的全名结果，匹配失败=空字符串
        nick (str):     输入的短昵称，返回原样
        lev (float):    查询的等级，1表示完全一致，<1表示相似性，用于判断是否加入昵称列表。
    """
    uid, who, lev = 0, "", 0.0
    if keywds in gw_nick_list:
        who = gw_nick[keywds]["uname"]
        lev = 1.0
        uid = gw_nick[keywds]["uid"]
        log.info(
            f"GuessUP: 搜索于 1-已有昵称列表, 关键词[{keywds}] ==> {who}({uid}) level=1.0"
        )
        return uid, who, keywds, lev

    if maybe := difflib.get_close_matches(keywds, gw_name_list):
        who = maybe[0]
        lev = max(
            difflib.SequenceMatcher(None, who, keywds).quick_ratio(),
            difflib.SequenceMatcher(None, keywds, who).quick_ratio(),
        )
        lev = float(int(lev * 100)) / 100
        uid = gw_user[who]["uid"]
        log.info(
            f"GuessUP: 搜索于 2-关注列表相似, 关键词[{keywds}] ==> {who}({uid}) level={lev}"
        )
    else:
        uid, who = await search_up_in_bili(keywds)
        if uid:
            lev = max(
                difflib.SequenceMatcher(None, who, keywds).quick_ratio(),
                difflib.SequenceMatcher(None, keywds, who).quick_ratio(),
            )
            lev = float(int(lev * 100)) / 100
            log.info(
                f"GuessUP: 搜索于 3-B站搜索页, 关键词[{keywds}] ==> {who}({uid}) level={lev}"
            )
        else:
            log.info(f"GuessUP: 所有途径搜索失败。关键词[{keywds}] ==> Nothing!")

    return uid, who, keywds, lev


def save_uname_nick(uid: str, uname: str, nick: str):
    """保存用户昵称

    Args:
        uid (str): 用户id
        uname (str): 用户名，没啥用，就二次确认一下
        nick (str): 要记录的昵称

    Returns:
        res (str/None):  错误信息,成功为空None
    """
    global up_group_info, gw_name_list, gw_nick_list, gw_user, gw_nick
    # 该昵称是否被人用过
    if nick in gw_nick_list:
        if gw_nick[nick]["uname"] == uname:
            return None
        log.info(
            f'保存昵称信息：失败，名称冲突。 {nick}已被 {gw_nick[nick]["uname"]}({gw_nick[nick]["uid"]}) 占用，{uname}无法使用。'
        )
        return f'该昵称已被 {gw_nick[nick]["uname"]}({gw_nick[nick]["uid"]}) 占用'
    if not up_group_info[uid].get("nick"):
        up_group_info[uid]["nick"] = []
    up_group_info[uid]["nick"].append(nick)
    try:
        with open(
            join(up_dir, "list.json"), "w", encoding="UTF-8"
        ) as f:  # 更新UP主列表
            json.dump(up_group_info, f, ensure_ascii=False)
    except Exception:
        up_group_info[uid]["nick"] = nick
        return "配置文件保存失败"
    # 更新内存中的配置
    for id in up_group_info:
        u = up_group_info[id]
        if u.get("nick"):
            gw_user[u["uname"]] = {"uid": u["uid"], "nick": u["nick"]}
            for n in u["nick"]:
                gw_nick[n] = {"uname": u["uname"], "uid": u["uid"]}
        else:
            gw_user[u["uname"]] = {"uid": u["uid"], "nick": []}
    gw_name_list = gw_user.keys()
    gw_nick_list = gw_nick.keys()
    log.info("保存昵称信息：成功")
    return None


def del_uname_nick(uid: str, uname: str, nick: str):
    """删除用户昵称。注意，本功能会验证uid，但不进行用户名验证，遇到不存在的用户名会出错。

    Args:
        uid (str): 用户id
        uname (str): 用户名，没啥用，就二次确认一下
        nick (str): 要记录的昵称

    Returns:
        res (str/None):  错误信息,成功为空None
    """
    global up_group_info, gw_name_list, gw_nick_list, gw_user, gw_nick
    if nick not in gw_nick_list:
        return "这个昵称未被使用。"
    if uid not in up_list:
        return "该用户未关注"
    if gw_nick[nick]["uname"] != uname:
        return "该用户无此昵称"
    up_group_info[uid]["nick"].remove(nick)
    try:
        with open(
            join(up_dir, "list.json"), "w", encoding="UTF-8"
        ) as f:  # 更新UP主列表
            json.dump(up_group_info, f, ensure_ascii=False)
    except Exception:
        up_group_info[uid]["nick"] = nick
        return "配置文件保存失败"
    # 更新内存中的配置
    for id in up_group_info:
        u = up_group_info[id]
        if u.get("nick"):
            gw_user[u["uname"]] = {"uid": u["uid"], "nick": u["nick"]}
            for n in u["nick"]:
                gw_nick[n] = {"uname": u["uname"], "uid": u["uid"]}
        else:
            gw_user[u["uname"]] = {"uid": u["uid"], "nick": []}
    gw_name_list = gw_user.keys()
    gw_nick_list = gw_nick.keys()
    return None


# ====================附加功能，外部请勿调用======================
# 每日清理垃圾，减少文件占用，减少内存占用
def clean_cache():
    global up_latest, up_dir
    img_cache = conf.getint("cache", "image_cache_days")
    dy_cache = conf.getint("cache", "dycard_cache_days")
    dy_flag = conf.getboolean("cache", "dycard_cache")
    if img_cache > 0:
        cache_clean_time_point = time.time() - img_cache * 3600 * 24
        dirname = ["image", "cover", "article_cover"]
        for t in dirname:
            for root, dirs, files in os.walk(join(curpath, f"res/cache/{t}")):
                for f in files:
                    full__path_file = join(root, f)
                    if getmtime(full__path_file) < cache_clean_time_point:
                        try:
                            os.remove(full__path_file)
                        except Exception:
                            log.error(f'Err while clean image cache: {f} in "{t}"!')
        log.info("Clean image cache finish!")
    if dy_cache > 0 and dy_flag:
        cache_clean_time_point = time.time() - dy_cache * 3600 * 24
        dirname = "dynamic_card"
        if exists(join(curpath, dirname)):
            for root, dirs, files in os.walk(join(curpath, f"res/cache/{dirname}")):
                for f in files:
                    full__path_file = join(root, f)
                    if getmtime(full__path_file) < cache_clean_time_point:
                        try:
                            os.remove(full__path_file)
                        except Exception:
                            log.error(
                                f'Err while clean dynamic cache: {f} in "{dirname}"!'
                            )
        log.info("Clean dynamic cache finish!")

    for uid in up_list:
        i = len(up_latest[uid])
        if i > 21:
            try:
                up_latest[uid] = up_latest[uid][(i - 21) :]  # 清理文件的同时清理内存
                up_history_write(uid)
            except Exception as e:
                log.error(f"Err while clean history: {uid}\n{e}")
    log.info("Clean uppers history finish!")


def m2hm(t: int):
    ms = t // 60
    return f"{int(ms//60)}h{int(ms%60)}m" if ms > 60 else f"{ms} minutes"


async def check_plugin_update():
    # 检查代码是否更新。由于现阶段代码会频繁更新，所以添加这个定期检查功能。
    # version.json内容：{"ver":"0.x.x", "date":"2022-07-01", "desc":["更新了版本检查功能，仅在日志里输出"]}
    way = conf.getint("common", "if_check_update")
    if way == 1:
        url = "https://gitee.com/kushidou/bili-notice-hoshino/raw/main/version.json"
    elif way == 2:
        url = "https://github.com/kushidou/bili-notice-hoshino/raw/main/version.json"
    else:
        return
    myverpath = join(curpath, "version.json")
    myver = "old"
    # 获取本地版本。不存在version文件则视为极旧版本
    if exists(myverpath):
        try:
            with open(myverpath, "r") as f:
                mytxt = json.load(f)
                myver = mytxt["ver"]
                log.info("例行检查更新,从version.json获取版本号")
        except Exception:
            myver = "old"
            log.info("从version.json获取版本号失败")
    else:
        log.info("例行检查更新,但是version.json不存在")
        with open(myverpath, "w", encoding="UTF-8") as f:  # 更新UP主列表
            json.dump({"ver": "old"}, f, ensure_ascii=False)

    try:
        async with httpx.AsyncClient(proxies=p) as client:
            res = await client.get(url=url, follow_redirects=True)
        # res = requests.get(url)
    except Exception:
        log.error("Check update failed! Please check your network.")
        return
    if res.status_code == 200:
        txt = json.loads(res.text)
        newver = txt["ver"]
        if newver != myver:
            date = txt["date"]
            desc = txt["desc"].replace("\n", "\n\t\t\t\t\t\t")
            log.info(
                f"bili-notice-hoshino插件已更新, 请至github主页拉取最新代码。\n \
                \t地址:  https://github.com/kushidou/bili-notice-hoshino  \n   \
                \t当前版本 {myver}, 最新版本号 {newver}, 更新时间{date}\n\
                \t更新内容:  {desc}"
            )
            return
    else:
        log.error(f"Check update failed! HTTP code = {res.status_code}")
        return


async def search_up_in_bili(keywds: str):
    global gcookies
    """到b站搜索up主，并返回最接近的信息

    Args:
        keywds (str): 输入的关键词

    Returns:
        uid (int):  搜索到的uid
        who (str):  对应的昵称
    """
    uid, who = 0, ""
    url = "https://api.bilibili.com/x/web-interface/search/type"
    header = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11; Redmi K30 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.54 Mobile Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    para = {"search_type": "bili_user", "keyword": keywds}
    if not gcookies:
        gcookies = await auth.update_cookies()
    try:
        async with httpx.AsyncClient(proxies=p) as client:
            res = await client.get(
                url=url, params=para, cookies=gcookies, headers=header
            )
        # res = requests.get(url=url, params=para, cookies=gcookies)
    except Exception as e:
        log.error(f"搜索UP主失败，原因为网络错误：{e}")
        return uid, who
    if res.status_code == 200:
        resj = json.loads(res.text)
        if resj["data"]["numResults"] != 0:
            usr = resj["data"]["result"][0]
            who = usr["uname"]
            uid = usr["mid"]
        else:
            log.error("搜索UP主失败，原因为 没有搜索到有关结果")
    else:
        log.error(f"搜索UP主失败，原因为 return code == {res.status_code}")
    return uid, who


async def cmd_blklist(group, para, right):
    rst = True
    msg = ""
    paranum = len(para)
    if paranum >= 3:
        uid = para[1]
        if uid not in up_list:
            msg = "UP主未关注,请检查uid!"
        else:
            fun = para[2]
            if fun == "add":
                if not right:
                    return False, "你没有权限这么做"
                if paranum > 3:
                    keys = para[3:]
                    try:
                        up_group_info[uid]["ad_keys"].extend(keys)
                        with open(
                            join(up_dir, "list.json"), "w", encoding="UTF-8"
                        ) as f:  # 更新UP主列表
                            json.dump(up_group_info, f, ensure_ascii=False)
                        msg = "添加成功."
                    except Exception:
                        msg = "添加失败"
            elif fun == "list":
                uname = up_group_info[uid]["uname"]
                msg = f'您已经为 {uname} 设置了以下过滤关键词：\r\n{up_group_info[uid]["ad_keys"]}'
            elif fun == "remove":
                if not right:
                    return False, "你没有权限这么做"
                if paranum > 3:
                    keys = para[3:]
                    erkeys = []
                    for wd in keys:
                        try:
                            up_group_info[uid]["ad_keys"].remove(wd)
                        except Exception:
                            erkeys.append(wd)
                    with open(
                        join(up_dir, "list.json"), "w", encoding="UTF-8"
                    ) as f:  # 更新UP主列表
                        json.dump(up_group_info, f, ensure_ascii=False)
                    msg = "移除成功。"
                    if erkeys:
                        msg += f"以下关键词移除失败，可能是没有这些关键词:\n{erkeys}"
    else:
        rst = False
        msg = "参数有误"
    return rst, msg


async def cmd_islucky(group, para, right):
    paranum = len(para)
    if not right:
        return False, "你没有权限这么做"
    if paranum != 3:
        return False, "参数有误"
    uid = para[1]
    if uid not in up_list:
        msg = "UP主未关注,请检查uid!"
    else:
        msg = f'已为 {up_group_info[uid]["uname"]} 更新抽奖开奖动态的设置。'
        fun = para[2]
        if fun.upper() == "TRUE":
            up_group_info[uid]["islucky"] = True
        elif fun.upper() == "FALSE":
            up_group_info[uid]["islucky"] = False
        else:
            msg = "参数错误，请重试。"
        with open(
            join(up_dir, "list.json"), "w", encoding="UTF-8"
        ) as f:  # 更新UP主列表
            json.dump(up_group_info, f, ensure_ascii=False)
    return True, msg


async def cmd_nick(group, para, right, fun):
    paranum = len(para)
    if not right:
        return False, "你没有权限这么做"
    if paranum == 3:
        u = para[1]
        n = para[2]
        if u.isdigit():
            uid = u
            uname = up_group_info[uid]["uname"]
        else:
            uid, uname, _, lev = await guess_who(u)
            if lev < 1:
                return False, "未找到该用户"
        if fun == "add":
            rst = save_uname_nick(str(uid), uname, n)
            print(rst)
            return True, rst or "成功"
        elif fun == "del":
            rst = del_uname_nick(str(uid), uname, n)
            print(rst)
            return True, rst or "成功"
    if paranum != 2 or fun != "list":
        return False, "参数有误"
    u = para[1]
    if u.isdigit():
        uid = u
        uname = up_group_info[uid]["uname"]
    else:
        uid, uname, _, lev = await guess_who(u)
        if lev < 1:
            return False, "未找到该用户"
    ruid = gw_user[uname]["uid"]
    rnick = gw_user[uname]["nick"]
    if len(rnick):
        msg = f"{uname}({ruid})的昵称有：\r\n"
        for n in rnick:
            msg += f"{n}\r\n"
    else:
        msg = f"{uname}({ruid}) 还没有昵称，请设置。\r\n"
    return True, msg[:-2]
