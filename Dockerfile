FROM python:3.12-slim
WORKDIR /opt/gold-rate-pipeline
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=src
ENTRYPOINT ["python", "-m", "goldpipe.pipeline"]
CMD ["run"]
