'''
from core.virtual_clock import clock
print(clock.now())
from utils.time_phrases import get_relative_time_phrase
virtual_ts = 113
real_ts = clock.to_real_time(virtual_ts)
phrase = get_relative_time_phrase(real_ts)
print(phrase)
'''
import datetime
now = datetime.datetime.now()
print(now)
print(now.date())
print(f"现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分")