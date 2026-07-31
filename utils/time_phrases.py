import datetime

def get_relative_time_phrase(real_timestamp: float) -> str:
    """根据记忆的真实Unix时间戳，返回相对时间短语"""
    now = datetime.datetime.now()
    dt = datetime.datetime.fromtimestamp(real_timestamp)
    delta = (now - dt).total_seconds()

    if delta < 60:
        return "刚刚"
    elif delta < 600:
        return "几分钟前"
    elif delta < 3600:
        return "几十分钟前"

    if dt.date() == now.date():
        hour = dt.hour
        if 5 <= hour < 12:
            return "今天上午"
        elif 12 <= hour < 14:
            return "今天中午"
        elif 14 <= hour < 18:
            return "今天下午"
        elif 0 <= hour < 5:
            return "今天凌晨"
        else:
            return "今天晚上"

    yesterday = now.date() - datetime.timedelta(days=1)
    if dt.date() == yesterday:
        hour = dt.hour
        if 5 <= hour < 12:
            return "昨天上午"
        elif 12 <= hour < 14:
            return "昨天中午"
        elif 14 <= hour < 18:
            return "昨天下午"
        elif 0 <= hour < 5:
            return "昨天凌晨"
        else:
            return "昨天晚上"

    if delta < 2 * 86400:
        return "前天"
    elif delta < 7 * 86400:
        days = int(delta // 86400)
        return f"{days}天前"

    return "曾经"