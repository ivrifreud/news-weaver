FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.11

COPY requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY . "${LAMBDA_TASK_ROOT}"

# Default handler — overridden per function in template.yaml
CMD ["lambda_pipeline.handler"]
