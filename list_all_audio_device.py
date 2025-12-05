import pjsua2 as pj


def list_audio_devices():
    """列舉所有音效裝置 (麥克風 & 喇叭)"""
    ep = pj.Endpoint()
    ep.libCreate()

    # 基本初始化
    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 0       # 關閉 log 避免干擾輸出
    ep_cfg.logConfig.consoleLevel = 0
    ep.libInit(ep_cfg)

    # 取得音效裝置管理器
    adm = ep.audDevManager()

    # 刷新裝置列表
    adm.refreshDevs()

    # 取得所有裝置
    dev_count = adm.getDevCount()
    print(f"找到 {dev_count} 個音效裝置:\n")
    print("=" * 60)

    capture_devices = []
    playback_devices = []

    for i in range(dev_count):
        info = adm.getDevInfo(i)
        device_info = {
            "id": i,
            "name": info.name,
            "driver": info.driver,
            "input_count": info.inputCount,
            "output_count": info.outputCount,
        }

        # 判斷是輸入(麥克風)還是輸出(喇叭)
        if info.inputCount > 0:
            capture_devices.append(device_info)
        if info.outputCount > 0:
            playback_devices.append(device_info)

    # 顯示麥克風(Capture)裝置
    print("\n🎤 麥克風 (Capture) 裝置:")
    print("-" * 60)
    for dev in capture_devices:
        print(f"  ID: {dev['id']:3d} | {dev['name']}")
        print(f"         驅動: {dev['driver']}, 輸入通道: {dev['input_count']}")

    # 顯示喇叭(Playback)裝置
    print("\n🔊 喇叭 (Playback) 裝置:")
    print("-" * 60)
    for dev in playback_devices:
        print(f"  ID: {dev['id']:3d} | {dev['name']}")
        print(f"         驅動: {dev['driver']}, 輸出通道: {dev['output_count']}")

    print("\n" + "=" * 60)

    # 顯示目前預設裝置
    cap_dev = adm.getCaptureDev()
    play_dev = adm.getPlaybackDev()

    # -1 = PJMEDIA_AUD_DEFAULT_CAPTURE_DEV (系統預設麥克風)
    # -2 = PJMEDIA_AUD_DEFAULT_PLAYBACK_DEV (系統預設喇叭)
    cap_str = "系統預設" if cap_dev == -1 else str(cap_dev)
    play_str = "系統預設" if play_dev == -2 else str(play_dev)

    print(f"\n目前 Capture (麥克風) 裝置: {cap_dev} ({cap_str})")
    print(f"目前 Playback (喇叭) 裝置: {play_dev} ({play_str})")
    print("\n📌 特殊 ID 說明:")
    print("   -1 = PJMEDIA_AUD_DEFAULT_CAPTURE_DEV (使用系統預設麥克風)")
    print("   -2 = PJMEDIA_AUD_DEFAULT_PLAYBACK_DEV (使用系統預設喇叭)")

    print("\n💡 提示: 在 isok.py 中可使用以下方式切換裝置:")
    print("   adm = ep.audDevManager()")
    print("   adm.setCaptureDev(device_id)   # 切換麥克風")
    print("   adm.setPlaybackDev(device_id)  # 切換喇叭")

    ep.libDestroy()

    return capture_devices, playback_devices


if __name__ == "__main__":
    list_audio_devices()