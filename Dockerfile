FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY publisher.py control_panel.py ./

ENTRYPOINT ["python", "-u", "publisher.py"]
