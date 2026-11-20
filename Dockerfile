# Works on Hugging Face Spaces (Docker), Railway, Fly.io, Google Cloud Run and any other Docker host.
FROM python:3.12-slim
WORKDIR /app
COPY greendata.py greendata_prototype.html hosted_server.py hosted_ui.html ./
RUN useradd -m app && chown -R app /app
USER app
ENV PORT=7860 GD_SANDBOX=/tmp/greendata_sandbox PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "hosted_server.py"]
