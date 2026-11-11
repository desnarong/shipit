"""Detect a project's language and generate a Dockerfile for it.

A Dockerfile in the repo always wins, so *any* language works; detection is
only a convenience for the common cases.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Detected:
    lang: str
    port: int
    dockerfile: str


def _cmd(default: str, start: str | None) -> str:
    return "CMD " + json.dumps(["sh", "-c", start or default])


def _exists(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _glob(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern))


def _node(root, port, start):
    return Detected("node", port, f"""FROM node:22-alpine
WORKDIR /app
COPY . .
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
RUN npm run build --if-present
ENV PORT={port} NODE_ENV=production
EXPOSE {port}
{_cmd("npm start", start)}
""")


_APP_RE = re.compile(r"^(\w+)\s*=\s*(Flask|FastAPI|Starlette|Quart)\(", re.M)


def _infer_python(root: Path, port: int) -> tuple[str, str] | None:
    """Guess (start command, extra pip packages) for Django / FastAPI / Flask projects."""
    deps = ""
    for f in ("requirements.txt", "pyproject.toml"):
        if (root / f).exists():
            deps += (root / f).read_text(encoding="utf-8", errors="ignore").lower()
    bind = f"0.0.0.0:{port}"
    if (root / "manage.py").exists():
        for wsgi in sorted(root.glob("*/wsgi.py")):
            return f"gunicorn {wsgi.parent.name}.wsgi:application -b {bind}", "gunicorn"
    for f in ("main.py", "app.py", "server.py", "api.py"):
        path = root / f
        if not path.exists():
            continue
        m = _APP_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
        if not m:
            continue
        target = f"{path.stem}:{m.group(1)}"
        if m.group(2) in ("FastAPI", "Starlette"):
            return f"uvicorn {target} --host 0.0.0.0 --port {port}", "uvicorn"
        return f"gunicorn {target} -b {bind}", "gunicorn"
    return None


def _python(root, port, start):
    extra = ""
    if start is None:
        guess = _infer_python(root, port)
        if guess:
            start, extra = guess
        else:
            for f in ("app.py", "main.py", "server.py"):
                if (root / f).exists():
                    start = f"python {f}"
                    break
            else:
                raise ValueError("Python project: set `start` in config (e.g. 'gunicorn app:app -b 0.0.0.0:8000')")
    pip_extra = f"RUN pip install --no-cache-dir {extra}\n" if extra else ""
    return Detected("python", port, f"""FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; \\
    elif [ -f pyproject.toml ]; then pip install --no-cache-dir .; fi
{pip_extra}ENV PORT={port} PYTHONUNBUFFERED=1
EXPOSE {port}
{_cmd(start, start)}
""")


def _go(root, port, start):
    return Detected("go", port, f"""FROM golang:1.23-alpine AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -o /out/app .
FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/app /app
ENV PORT={port}
EXPOSE {port}
{_cmd("/app", start)}
""")


def _rust(root, port, start):
    name = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8")).get("package", {}).get("name")
    if not name:
        raise ValueError("Cargo.toml has no [package].name (workspace?) - provide your own Dockerfile")
    return Detected("rust", port, f"""FROM rust:1-slim AS build
WORKDIR /src
COPY . .
RUN cargo build --release
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/{name} /app
ENV PORT={port}
EXPOSE {port}
{_cmd("/app", start)}
""")


def _java_maven(root, port, start):
    return Detected("java-maven", port, f"""FROM maven:3-eclipse-temurin-21 AS build
WORKDIR /src
COPY . .
RUN mvn -q -B package -DskipTests
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/target/*.jar /tmp/jars/
RUN cp "$(ls /tmp/jars/*.jar | grep -v original | head -1)" /app/app.jar
ENV PORT={port} SERVER_PORT={port}
EXPOSE {port}
{_cmd("java -jar /app/app.jar", start)}
""")


def _java_gradle(root, port, start):
    return Detected("java-gradle", port, f"""FROM gradle:8-jdk21 AS build
WORKDIR /src
COPY . .
RUN gradle build -x test --no-daemon
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/build/libs/ /tmp/jars/
RUN cp "$(ls /tmp/jars/*.jar | grep -v plain | head -1)" /app/app.jar
ENV PORT={port} SERVER_PORT={port}
EXPOSE {port}
{_cmd("java -jar /app/app.jar", start)}
""")


def _php(root, port, start):
    return Detected("php", 80, """FROM php:8.3-apache
RUN a2enmod rewrite
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /var/www/html
COPY . .
RUN if [ -f composer.json ]; then composer install --no-dev --no-interaction; fi
EXPOSE 80
""")


def _ruby(root, port, start):
    if start is None:
        if not (root / "config.ru").exists():
            raise ValueError("Ruby project: set `start` in config")
        start = f"bundle exec rackup -o 0.0.0.0 -p {port}"
    return Detected("ruby", port, f"""FROM ruby:3.3-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN bundle install
ENV PORT={port}
EXPOSE {port}
{_cmd(start, start)}
""")


def _dotnet(root, port, start):
    proj = (_glob(root, "*.csproj") or _glob(root, "*.fsproj"))[0].stem
    return Detected("dotnet", port, f"""FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY . .
RUN dotnet publish -c Release -o /out
FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /out .
ENV ASPNETCORE_URLS=http://+:{port}
EXPOSE {port}
{_cmd(f"dotnet {proj}.dll", start)}
""")


def _elixir(root, port, start):
    return Detected("elixir", port, f"""FROM elixir:1.17-alpine
WORKDIR /app
RUN mix local.hex --force && mix local.rebar --force
COPY . .
ENV MIX_ENV=prod PORT={port}
RUN mix deps.get --only prod && mix compile
EXPOSE {port}
{_cmd("mix phx.server", start)}
""")


def _static(root, port, start):
    return Detected("static", 80, """FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
""")


# Order matters: first match wins.
_RULES = [
    (lambda r: _exists(r, "package.json"), _node),
    (lambda r: _exists(r, "go.mod"), _go),
    (lambda r: _exists(r, "Cargo.toml"), _rust),
    (lambda r: _exists(r, "pom.xml"), _java_maven),
    (lambda r: _exists(r, "build.gradle", "build.gradle.kts"), _java_gradle),
    (lambda r: _exists(r, "composer.json") or _glob(r, "*.php"), _php),
    (lambda r: _exists(r, "Gemfile"), _ruby),
    (lambda r: _glob(r, "*.csproj") or _glob(r, "*.fsproj"), _dotnet),
    (lambda r: _exists(r, "mix.exs"), _elixir),
    (lambda r: _exists(r, "requirements.txt", "pyproject.toml", "app.py", "main.py"), _python),
    (lambda r: _exists(r, "index.html"), _static),
]


def detect(root: str | Path, port: int = 8080, start: str | None = None) -> Detected:
    root = Path(root)
    for matches, build in _RULES:
        if matches(root):
            return build(root, port, start)
    raise ValueError(
        f"Could not detect language in {root}. Add a Dockerfile to the repo - any language works with one."
    )
