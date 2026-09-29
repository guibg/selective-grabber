FROM python:3.12-alpine

WORKDIR /app
COPY selective_grabber.py /app/selective_grabber.py

VOLUME ["/config"]

CMD ["python3", "/app/selective_grabber.py"]
