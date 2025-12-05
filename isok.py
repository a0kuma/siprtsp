import time
import os
from pathlib import Path

import pjsua2 as pj


# Small .env loader (simple, no external deps)
def load_dotenv(dotenv_path=".env"):
    p = Path(dotenv_path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


# try to load `.env` in project root
load_dotenv()


# =========================
# SIP 帳號設定
# =========================
# These values can be provided via environment or `./.env`.
SIP_DOMAIN = os.getenv("SIP_DOMAIN")
SIP_USER = os.getenv("SIP_USER")          # 這支 py 是 5004
SIP_PASSWD = os.getenv("SIP_PASSWD")

# 本機 SIP 來源 port
# 5060 會跟別的東西打架就改 0，讓 OS 自動選
LOCAL_SIP_PORT = 0

# =========================
# 全域 calls map
# =========================
# key: call.id  (int)
# val: MyCall 實例
g_calls = {}


class MyCall(pj.Call):
    def __init__(self, account, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(account, call_id)
        self.acc = account
        self.call_media = None
        self.playback_media = None
        self.capture_media = None

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"[call {ci.id}] State: {ci.stateText} ({ci.lastReason})")

        # 通話建立
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            print(f"[call {ci.id}] CONFIRMED, bridge to system audio")

            try:
                media = self.getMedia(0)
            except pj.Error:
                print(f"[call {ci.id}] getMedia(0) failed")
                return

            audio_media = None
            if media is not None:
                try:
                    audio_media = pj.AudioMedia.typecastFromMedia(media)
                except (pj.Error, TypeError):
                    audio_media = None

            if audio_media is not None:
                adm = pj.Endpoint.instance().audDevManager()
                playback = adm.getPlaybackDevMedia()
                capture = adm.getCaptureDevMedia()

                if playback is None or capture is None:
                    print(f"[call {ci.id}] Audio devices not available (playback={playback}, capture={capture})")
                    return

                try:
                    audio_media.startTransmit(playback)
                    capture.startTransmit(audio_media)
                except pj.Error as err:
                    print(f"[call {ci.id}] Failed to bridge audio: {err}")
                    return

                self.call_media = audio_media
                self.playback_media = playback
                self.capture_media = capture
                print(f"[call {ci.id}] Audio bridged to default devices")
            else:
                print(f"[call {ci.id}] Media[0] could not be treated as AudioMedia")

        # 通話結束
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"[call {ci.id}] DISCONNECTED, cleanup")

            if self.call_media and self.playback_media:
                try:
                    self.call_media.stopTransmit(self.playback_media)
                except pj.Error:
                    pass

            if self.capture_media and self.call_media:
                try:
                    self.capture_media.stopTransmit(self.call_media)
                except pj.Error:
                    pass

            self.call_media = None
            self.playback_media = None
            self.capture_media = None

            # 從全域 map 移除，讓 Python 可以 GC 掉這個 MyCall
            if ci.id in g_calls:
                del g_calls[ci.id]
                print(f"[call {ci.id}] Removed from g_calls")


class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()

    def onRegState(self, prm):
        ai = self.getInfo()
        reason = getattr(ai, "regStatusText", None)
        if reason is None:
            reason = getattr(ai, "regReason", "")
        print(f"[acc] Reg state: {ai.regIsActive} ({ai.regStatus} {reason})")

    def onIncomingCall(self, prm):
        # 建立 MyCall 實例
        call = MyCall(self, prm.callId)
        ci = call.getInfo()
        print(f"[call {ci.id}] Incoming from: {ci.remoteUri}")

        # ⭐ 關鍵：把 call 存到全域 map 裡，避免 callback 結束被 GC
        g_calls[ci.id] = call

        # 自動接聽 200 OK
        op = pj.CallOpParam()
        op.statusCode = 200
        print(f"[call {ci.id}] Auto-answer 200 OK")
        call.answer(op)


def main():
    ep = pj.Endpoint()
    ep.libCreate()

    # Endpoint config & log
    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 4
    ep_cfg.logConfig.consoleLevel = 4

    ep.libInit(ep_cfg)

    # Transport 設定
    sip_cfg = pj.TransportConfig()
    sip_cfg.port = LOCAL_SIP_PORT
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, sip_cfg)

    ep.libStart()
    print(f"[ep] PJSUA2 started, listening on UDP port {sip_cfg.port}")

    # Account 設定
    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{SIP_USER}@{SIP_DOMAIN}"
    acc_cfg.regConfig.registrarUri = f"sip:{SIP_DOMAIN}"

    # 認證資訊
    cred = pj.AuthCredInfo("digest", SIP_DOMAIN, SIP_USER, 0, SIP_PASSWD)
    acc_cfg.sipConfig.authCreds.append(cred)

    # 建立帳號
    acc = MyAccount()
    acc.create(acc_cfg)
    print(f"[acc] Created and registering as {acc_cfg.idUri}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ep] Ctrl+C, shutting down...")
    finally:
        ep.libDestroy()
        print("[ep] libDestroy done.")


if __name__ == "__main__":
    main()