# voice-service

语音那半边的两个独立服务。跟 dwell 后端（Flask，8888）是不同进程，
只通过 HTTP 说话。

```
手机 Safari
  ↓ https
Cloudflare → cloudflared → nginx:8070
                              ├─ /            → Flask:8888   聊天、前端
                              └─ /api/voice   → voice:8021   录音上传 / TTS
                                                     ↓
                                               stt:8022        转写
                                                     ↓
                                          阿里云 gummy-realtime-v1
```

## 为什么不合进 Flask

TTS 和 STT 都要等外部服务好几秒。Flask 同步模型下那一等会把请求线程
占住，聊天跟着卡。分开跑，一边挂了另一边不受影响。

## 文件

| 文件 | 干什么 |
|---|---|
| `main.py` | voice 服务入口，把 `voice_routes` 三个端点挂上 + token 鉴权 |
| `stt_service.py` | 转写服务，ffmpeg 转 PCM 后推给阿里云 |
| `systemd/*.service` | 两个 unit，拷到 `/etc/systemd/system/` |
| `voice.env.example` | 环境变量模板，拷成 `/etc/voice.env` 再填真值 |

`voice_service.py` 和 `voice_routes.py` 不在这里 —— 那两个来自
[zaochuanyitian/voice](https://github.com/zaochuanyitian/voice)，原样用。

## 部署

第一次：

```bash
sudo mkdir -p /opt/voice && cd /opt/voice
sudo git clone https://github.com/zaochuanyitian/voice.git repo
sudo cp repo/server/voice_service.py repo/server/voice_routes.py .
sudo python3 -m venv venv
sudo ./venv/bin/pip install fastapi uvicorn python-multipart certifi dashscope
sudo apt install -y ffmpeg
```

把这个目录里的两个 py 拷过去，环境变量和 unit 就位：

```bash
sudo cp voice-service/main.py voice-service/stt_service.py /opt/voice/
sudo cp voice-service/voice.env.example /etc/voice.env   # 然后填真值
sudo chmod 600 /etc/voice.env
sudo cp voice-service/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice stt
```

以后更新只要：

```bash
cd /root/dwell-on-something && git pull
sudo cp voice-service/main.py voice-service/stt_service.py /opt/voice/
sudo systemctl restart voice stt
```

## nginx

往已有的 server 里加一段，**别新开站点** —— 跟前端同源可以省掉 CORS：

```nginx
location /api/voice {
    proxy_pass http://127.0.0.1:8021;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    client_max_body_size 16m;      # 一条录音上限 12MB，留点余量
    proxy_request_buffering off;
    proxy_read_timeout 60s;
    proxy_buffering off;
}
```

`client_max_body_size` 不加的话 nginx 默认只收 1MB，两分钟的录音直接 413。

## 查活没活

```bash
curl -s http://127.0.0.1:8021/healthz    # elevenlabs / edge_tts / stt 三个开关
curl -s http://127.0.0.1:8022/healthz    # 转写服务和 key
journalctl -u voice -n 30 --no-pager
journalctl -u stt -n 30 --no-pager
```

想验 TTS 但不想烧字符（ElevenLabs 按字符计费），就发空文本：回 400
说明 token 过了、请求进到了业务逻辑，只是在调 ElevenLabs 之前就被挡下。

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8021/api/voice/tts \
  -H 'Content-Type: application/json' -H "X-Voice-Token: $TOKEN" -d '{"text":""}'
```

## 几个踩过的坑

- **`python-multipart` 不能漏**。FastAPI 处理文件上传靠它，新版不再自动装，
  漏了 `/api/voice/message` 直接报错。
- **`SAY_VOICE` 要留空**。那一档调 `/usr/bin/say`，macOS 限定，Linux 上
  必然失败。不关就是每次白白等 20 秒超时。
- **ffmpeg 要走临时文件，不能用 stdin**。mp4 的 moov box 在文件尾，
  管道里没法 seek，会报 "moov atom not found"。
- **转写只收 `is_sentence_end` 的结果**。中间结果是不断修正的全量文本，
  那些也拼进去就是一堆叠字。
- **`compare_digest` 要比字节**。对 str 参数它要求纯 ASCII，别人往 header
  里塞中文会抛 TypeError，本该 401 的变成 500。
- **通话里浏览器自带的听写跑不了**。`webkitSpeechRecognition` 跟
  `MediaRecorder` 抢同一个音频会话，iOS 上只能一个赢。这就是为什么
  非得有服务端 STT：没它通话里模型只能收到一个 `[voice · 0:05]` 空壳。
