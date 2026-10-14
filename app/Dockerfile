# Hugging Face Space (Docker SDK). Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY as Space secrets
# (a read-only key), and SWOT_STORE / AWS_REGION as variables if they differ from the defaults.
FROM python:3.13-slim
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user server.py swot_view.py ./
COPY --chown=user static ./static
ENV PATH=/home/user/.local/bin:$PATH AWS_REGION=us-east-1
EXPOSE 7860
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
