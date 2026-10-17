FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY publisher.py control_panel.py streams.default.json ./

ENTRYPOINT ["python", "-u", "publisher.py"]
