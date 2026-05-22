import serial, time

for port in ['COM4', 'COM5']:
    print(f"\n--- {port} ---")
    try:
        s = serial.Serial(port, 115200, timeout=3)
        time.sleep(2)
        while s.in_waiting:
            print("起動メッセージ:", repr(s.readline()))
        s.write(b'PING\n')
        resp = s.readline()
        print("PING応答:", repr(resp))
        if resp.strip() == b'OK':
            print(f"  → このポートを使用: {port}")
        s.close()
    except Exception as e:
        print(f"エラー: {e}")
