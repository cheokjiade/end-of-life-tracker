# Multi-stage build with global ARG defaults and stage aliases.
ARG PYTHON_VERSION=3.12

FROM --platform=linux/amd64 python:${PYTHON_VERSION}-slim AS builder
WORKDIR /app
COPY . .

FROM builder AS tester
RUN python -m compileall .

FROM python:3.12.4-alpine AS runtime
WORKDIR /app
COPY --from=builder /app /app

FROM node:20 AS tools
