# Dashcam Integration

Vendor-independent JT/T 808-2013 + JT/T 1078-2016 server for the Pictor T98 (rebadged H20P)
dual-channel AI dashcam. Pulls live video and ADAS/DMS alarms with no Pictor cloud dependency.

## Pipeline

```
dashcam --4G/TCP--> jt_server.py --H.264--> ffmpeg --RTSP--> MediaMTX --> RTSP / HLS
         :6608 signalling                                               :8554 / :8888
         :6609 media                        telemetry (SSE) -----------> :8099
```

The device dials out to the server; nothing connects inward to the dashcam.

## Files

| File | Purpose |
|---|---|
| `jt_server.py` | JT808 signalling, JT1078 reassembly, ffmpeg publish, telemetry API |
| `viewer.html` | Dashboard — both channels + GPS/alarm telemetry |
| `start-viewer.ps1` | Opens SSH tunnels and launches the dashboard |
| `mediamtx.yml` | MediaMTX config (VM only) |

## Deployment

VM: `azureuser@20.197.4.129`, path `/var/www/Dashcam_Integration/`

```bash
sudo systemctl status dashcam-jtserver dashcam-mediamtx
sudo journalctl -u dashcam-jtserver -f | grep "LOC "
```

Device ID `871080028908` (last 12 digits of IMEI), auth code `SENTINEL01`.

## Ports

| Port | Bind | Use |
|---|---|---|
| 6608 / 6609 | public | JT808 signalling / JT1078 media |
| 8554 | public | RTSP — `rtsp://127.0.0.1:8554/<device>_ch<n>` |
| 8888 | loopback | HLS for the browser |
| 8099 | loopback | Telemetry SSE (`/events`) + JSON (`/api/state`) |

## Viewing

```powershell
.\start-viewer.ps1
```

Tunnels 8888 + 8099, opens the dashboard, holds until Ctrl+C.

For OpenCV:

```python
cap = cv2.VideoCapture("rtsp://127.0.0.1:8554/871080028908_ch1")
```

## Device commands

Write one word into `control.cmd` on the VM; it fires within ~10s, no service restart
(restarting drops the video streams).

```bash
echo query   > control.cmd   # 0x8104 — dump all terminal parameters
echo gps     > control.cmd   # 0x8103 — set reporting strategy/interval
echo restart > control.cmd   # 0x8105 — reboot (this firmware ignores it)
```

## Notes

- MediaMTX paths exist only while ffmpeg is publishing — `ffprobe` fails when the device is offline.
- Restarting MediaMTX drops the publishers; restart `dashcam-jtserver` afterwards.
- GPS cold start needs open sky. `sats=0` with a frozen position means no fix; look for
  `fix=True` with `sats>=6`. Device clock only corrects itself once locked.
- The server IP is stored on the device (param `0x0013`). Keep the Azure public IP **static** —
  if it changes, the dashcam dials a dead address and never reconnects.
- Azure auto-shutdown will stop the VM on a schedule and silently kill the feed.
