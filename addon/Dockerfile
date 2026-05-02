ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip py3-numpy

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY proxy_server.py gemini_session.py ha_client.py protocol.py /app/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
