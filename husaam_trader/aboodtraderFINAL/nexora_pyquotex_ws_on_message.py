"""
تصحيح pyquotex: في client.on_message يُستدعى message.get() بعد json.loads
حين يكون message نصًا أو نوعًا غير dict (مثلاً مع جسر WS)، فيحدث:
'str' object has no attribute 'get'

المصدر: cleitonleonel/pyquotex pyquotex/ws/client.py — مع حماية isinstance(message, dict)
للفروع التي تستخدم _temp_status (settings/history).
"""
import json
import logging
import time

logger = logging.getLogger(__name__)


def _rows_look_like_instruments(rows) -> bool:
    """صفوف أدوات Quotex: قائمة من القوائم، عادة ≥10–15 عمودًا."""
    if not isinstance(rows, list) or not rows:
        return False
    r0 = rows[0]
    return isinstance(r0, (list, tuple)) and len(r0) >= 10


def _parse_engineio_json(msg_str: str):
    """
    رسائل Socket.IO عبر Engine.IO غالبًا `42["event", data]` — أحيانًا يكفي حرف واحد
    وأحيانًا حرفان قبل JSON. نجرّب 1 و2 ثم السلسلة كاملة.
    """
    if len(msg_str) <= 1:
        return None
    for skip in (1, 2):
        if len(msg_str) <= skip:
            continue
        try:
            return json.loads(msg_str[skip:])
        except (ValueError, TypeError):
            continue
    try:
        return json.loads(msg_str)
    except (ValueError, TypeError):
        return None


def _nexora_try_assign_instruments(api, message) -> None:
    """
    stable_api.get_instruments ينتظر api.instruments (قائمة غير فارغة).
    النسخة الأصلية تعتمد على substring "call"/"put" داخل str(message) — قد لا يتحقق
    مع تنسيقات جديدة. نملأ القائمة من حدث instruments/list أو من شكل الصفوف.
    """
    if message is None:
        return
    if isinstance(message, list) and len(message) >= 2:
        head = message[0]
        if isinstance(head, str) and "instruments" in head.lower():
            payload = message[1]
            if isinstance(payload, list) and _rows_look_like_instruments(payload):
                api.instruments = payload
                return
    if isinstance(message, list) and _rows_look_like_instruments(message):
        api.instruments = message
        return
    if "call" in str(message) or "put" in str(message):
        api.instruments = message


def on_message(self, wss, msg):
    """نسخة آمنة من WebsocketClient.on_message."""
    self.state.ssl_Mutual_exclusion = True
    current_time = time.localtime()
    if current_time.tm_sec in [0, 5, 10, 15, 20, 30, 40, 50]:
        self.wss.send('42["tick"]')
    try:
        if "authorization/reject" in str(msg):
            logger.warning("Token rejected, making automatic reconnection.")
            self.state.check_rejected_connection = 1
        elif "s_authorization" in str(msg):
            self.state.check_accepted_connection = 1
            self.state.check_rejected_connection = 0
        elif "instruments/list" in str(msg):
            self.state.started_listen_instruments = True

        msg_str = msg.decode("utf-8", errors="ignore") if isinstance(msg, bytes) else str(msg)
        message = msg_str

        if len(msg_str) > 1:
            logger.debug(msg_str[:500])
            message_json = _parse_engineio_json(msg_str)
            if message_json is not None:
                message = message_json
                self.api.wss_message = message
                _nexora_try_assign_instruments(self.api, message)
            if isinstance(message, dict):
                if message.get("signals"):
                    time_in = message.get("time")
                    for i in message["signals"]:
                        try:
                            self.api.signal_data[i[0]] = {}
                            self.api.signal_data[i[0]][i[2]] = {}
                            self.api.signal_data[i[0]][i[2]]["dir"] = i[1][0]["signal"]
                            self.api.signal_data[i[0]][i[2]]["duration"] = i[1][0]["timeFrame"]
                        except (KeyError, IndexError, TypeError):
                            self.api.signal_data[i[0]] = {}
                            self.api.signal_data[i[0]][time_in] = {}
                            self.api.signal_data[i[0]][time_in]["dir"] = i[1][0][1]
                            self.api.signal_data[i[0]][time_in]["duration"] = i[1][0][0]
                elif message.get("liveBalance") or message.get("demoBalance"):
                    self.api.account_balance = message
                elif message.get("position"):
                    self.api.top_list_leader = message
                elif len(message) == 1 and message.get("profit", -1) > -1:
                    self.api.profit_today = message
                elif message.get("index"):
                    self.api.historical_candles = message
                    if message.get("closeTimestamp"):
                        self.api.timesync.server_timestamp = message.get("closeTimestamp")
                if message.get("pending"):
                    self.api.pending_successful = message
                    self.api.pending_id = message["pending"]["ticket"]
                elif message.get("id") and not message.get("ticket"):
                    self.api.buy_successful = message
                    self.api.buy_id = message["id"]
                    if message.get("closeTimestamp"):
                        self.api.timesync.server_timestamp = message.get("closeTimestamp")
                elif message.get("ticket") and not message.get("id"):
                    self.api.sold_options_respond = message
                elif message.get("deals"):
                    for get_m in message["deals"]:
                        self.api.profit_in_operation = get_m["profit"]
                        get_m["win"] = True if message["profit"] > 0 else False
                        get_m["game_state"] = 1
                        self.api.listinfodata.set(
                            get_m["win"],
                            get_m["game_state"],
                            get_m["id"],
                        )
                elif message.get("isDemo") and message.get("balance"):
                    self.api.training_balance_edit_request = message
                elif message.get("error"):
                    self.state.websocket_error_reason = message.get("error")
                    self.state.check_websocket_if_error = True
                    if self.state.websocket_error_reason == "not_money":
                        self.api.account_balance = {"liveBalance": 0}
                elif not message.get("list") == []:
                    self.api.wss_message = message
        if str(message) == "41":
            logger.info("Disconnection event triggered by the platform, causing automatic reconnection.")
            self.state.check_websocket_if_connect = 0
        if "51-" in str(message):
            self.api._temp_status = str(message)
        elif isinstance(message, dict) and self.api._temp_status == (
            """451-["settings/list",{"_placeholder":true,"num":0}]"""
        ):
            self.api.settings_list = message
            self.api._temp_status = ""
        elif isinstance(message, dict) and self.api._temp_status == (
            """451-["history/list/v2",{"_placeholder":true,"num":0}]"""
        ):
            if message.get("asset") == self.api.current_asset:
                self.api.candles.candles_data = message["history"]
                self.api.candle_v2_data[message["asset"]] = message
                self.api.candle_v2_data[message["asset"]]["candles"] = [
                    {
                        "time": candle[0],
                        "open": candle[1],
                        "close": candle[2],
                        "high": candle[3],
                        "low": candle[4],
                        "ticks": candle[5],
                    }
                    for candle in message["candles"]
                ]
        elif isinstance(message, list) and len(message) > 0 and isinstance(message[0], list) and len(message[0]) == 4:
            result = {
                "time": message[0][1],
                "price": message[0][2],
            }
            self.api.realtime_price[message[0][0]].append(result)
            self.api.realtime_candles[self.api.current_asset] = message[0]
        elif isinstance(message, list) and len(message) > 0 and isinstance(message[0], list) and len(message[0]) == 2:
            for i in message:
                result = {
                    "sentiment": {
                        "sell": 100 - int(i[1]),
                        "buy": int(i[1]),
                    }
                }
                self.api.realtime_sentiment[i[0]] = result
    except Exception as e:
        logger.error("Unhandled error in on_message: %s", e)
    self.state.ssl_Mutual_exclusion = False


on_message._nexora_pyquotex_on_message_fix = True
