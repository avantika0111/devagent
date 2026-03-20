"""
agents/docker_agent.py

Verifies the project builds and runs correctly in a container.

Lifecycle:
    1. build_image   - docker build, records build time and image size
    2. start_container - run the image with test env vars
    3. check_health  - poll the health endpoint until healthy or timeout
    4. read_logs     - capture container logs for the report
    5. stop_container - clean up regardless of outcome

Docker unavailability:
    If Docker daemon is not running or Docker is not installed,
    the agent logs a clear warning, posts a WARNING finding (not CRITICAL),
    marks docker_skipped=True in memory, and returns success so the
    pipeline continues. This matches the project decision: warn but continue.

Reads from memory:
    plan_id             used to name the test container

Writes to memory:
    docker_passed       True if build + health check succeeded
    docker_skipped      True if Docker was unavailable
    docker_image_size   image size in MB
    docker_build_time   seconds to build
    docker_summary      human-readable outcome
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from core.base_agent import BaseAgent, ToolError
from core.memory import Finding, Severity

logger = logging.getLogger(__name__)

# Default timeout values (overridden by docker.md config)
DEFAULT_STARTUP_TIMEOUT = 30
DEFAULT_HEALTH_ENDPOINT  = "/health"
DEFAULT_HEALTH_PORT      = 8080
POLL_INTERVAL            = 2   # seconds between health polls


class DockerAgent(BaseAgent):
    """
    Builds and verifies the Docker container.

    Tools:
        check_docker_available  verify Docker daemon is reachable
        build_image             docker build with timing
        start_container         docker run with test env vars
        check_health            poll health endpoint until ready
        read_logs               get container stdout/stderr
        stop_container          stop and remove the container
        post_report             finalise result in memory
    """

    name = "docker_agent"

    def __init__(self, config, memory):
        super().__init__(config, memory)
        self._container_id: Optional[str] = None
        self._image_tag:    Optional[str] = None

    @property
    def system_prompt(self) -> str:
        return """\
You are a DevOps engineer verifying that the project builds and runs correctly.

## Process (follow in order)

1. Call check_docker_available first.
   - If Docker is not available: call post_report with skipped=true. Stop here.
   - If Docker is available: continue.

2. Call build_image to build the Docker image.
   - If build fails: read the error, call post_report with the failure. Stop here.

3. Call start_container to run the image.
   - Pass any required test environment variables.

4. Call check_health to verify the container is healthy.
   - The health endpoint and timeout come from docker.md rules.

5. Call read_logs to capture container output for the report.

6. Call stop_container to clean up.

7. Call post_report with the full outcome summary.

## Rules

- Always call stop_container before finishing, even if an earlier step failed.
- Never leave containers running after the agent finishes.
- Use the health endpoint configured in docker.md, not a hardcoded one.
- If the health check times out, that is a FAILURE - report it clearly.
- Image tag format: devagent-{plan_id}-test (lowercase, no spaces).
"""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "check_docker_available",
                "description": "Check if Docker daemon is running and reachable",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "build_image",
                "description": "Build the Docker image from the project Dockerfile",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "dockerfile": {
                            "type": "string",
                            "description": "Path to Dockerfile relative to project root",
                            "default": "Dockerfile"
                        },
                        "build_args": {
                            "type": "object",
                            "description": "Build arguments as key-value pairs",
                            "default": {}
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "start_container",
                "description": "Start a container from the built image",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "env_vars": {
                            "type": "object",
                            "description": "Environment variables to pass to the container",
                            "default": {}
                        },
                        "port": {
                            "type": "integer",
                            "description": "Host port to bind container port 8080 to",
                            "default": 18080
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "check_health",
                "description": "Poll the health endpoint until healthy or timeout",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "endpoint": {
                            "type": "string",
                            "description": "Health check path e.g. /health",
                            "default": "/health"
                        },
                        "port": {
                            "type": "integer",
                            "description": "Port to hit for health check",
                            "default": 18080
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Max seconds to wait for healthy response",
                            "default": 30
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "read_logs",
                "description": "Read stdout/stderr from the running container",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tail": {
                            "type": "integer",
                            "description": "Number of log lines to return",
                            "default": 50
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "stop_container",
                "description": "Stop and remove the test container. Always call this at the end.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "post_report",
                "description": "Finalise the Docker check result in memory",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "passed": {
                            "type": "boolean",
                            "description": "True if build + health check succeeded"
                        },
                        "skipped": {
                            "type": "boolean",
                            "description": "True if Docker was not available",
                            "default": False
                        },
                        "summary": {
                            "type": "string",
                            "description": "Human-readable outcome description"
                        }
                    },
                    "required": ["passed", "summary"]
                }
            }
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "check_docker_available":
            return self._check_docker_available()

        elif tool_name == "build_image":
            return self._build_image(
                dockerfile=tool_input.get("dockerfile", "Dockerfile"),
                build_args=tool_input.get("build_args", {}),
            )

        elif tool_name == "start_container":
            return self._start_container(
                env_vars=tool_input.get("env_vars", {}),
                port=tool_input.get("port", 18080),
            )

        elif tool_name == "check_health":
            return self._check_health(
                endpoint=tool_input.get("endpoint", DEFAULT_HEALTH_ENDPOINT),
                port=tool_input.get("port", 18080),
                timeout=tool_input.get("timeout_seconds", DEFAULT_STARTUP_TIMEOUT),
            )

        elif tool_name == "read_logs":
            return self._read_logs(tail=tool_input.get("tail", 50))

        elif tool_name == "stop_container":
            return self._stop_container()

        elif tool_name == "post_report":
            return self._post_report(
                passed=tool_input["passed"],
                skipped=tool_input.get("skipped", False),
                summary=tool_input["summary"],
            )

        raise ToolError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _check_docker_available(self) -> dict:
        try:
            import docker
            client = docker.from_env()
            client.ping()
            version = client.version().get("Version", "unknown")
            logger.info(f"[{self.name}] Docker available: v{version}")
            return {"available": True, "version": version}

        except ImportError:
            self._handle_docker_unavailable(
                "docker Python SDK not installed. Run: pip install docker"
            )
            return {"available": False, "reason": "docker SDK not installed"}

        except Exception as e:
            self._handle_docker_unavailable(
                f"Docker daemon not reachable: {e}. "
                "Ensure Docker Desktop is running."
            )
            return {"available": False, "reason": str(e)}

    def _build_image(self, dockerfile: str, build_args: dict) -> dict:
        client    = self._docker_client()
        plan_id   = (self.memory.get("plan_id") or "dev").lower().replace(" ", "-")
        tag       = f"devagent-{plan_id}-test"
        self._image_tag = tag

        dockerfile_path = self.config.project_root / dockerfile
        if not dockerfile_path.exists():
            raise ToolError(
                f"Dockerfile not found at {dockerfile_path}. "
                "Create a Dockerfile in the project root."
            )

        logger.info(f"[{self.name}] Building image: {tag}")
        start = time.time()

        try:
            image, logs = client.images.build(
                path=str(self.config.project_root),
                dockerfile=dockerfile,
                tag=tag,
                buildargs=build_args,
                rm=True,        # remove intermediate containers
                forcerm=True,
            )
            elapsed  = round(time.time() - start, 1)
            size_mb  = round(image.attrs.get("Size", 0) / 1_000_000, 1)

            self.memory.set("docker_build_time",  elapsed)
            self.memory.set("docker_image_size",  size_mb)

            logger.info(
                f"[{self.name}] Build succeeded: {tag} "
                f"({size_mb}MB, {elapsed}s)"
            )
            return {
                "success":    True,
                "tag":        tag,
                "size_mb":    size_mb,
                "elapsed_s":  elapsed,
            }

        except Exception as e:
            error_msg = str(e)
            self.memory.add_finding(Finding.create(
                agent=self.name,
                severity=Severity.HIGH,
                title="Docker build failed",
                description=error_msg[:400],
                suggestion="Fix the Dockerfile or build dependencies",
                source="docker:build-failure",
            ))
            raise ToolError(f"Docker build failed: {error_msg[:200]}")

    def _start_container(self, env_vars: dict, port: int) -> dict:
        client = self._docker_client()
        if not self._image_tag:
            raise ToolError("No image built yet. Call build_image first.")

        # Merge with safe test defaults
        env = {
            "DEVAGENT_ENV":  "test",
            "LOG_LEVEL":     "INFO",
            **env_vars,
        }

        logger.info(f"[{self.name}] Starting container from {self._image_tag}")

        try:
            container = client.containers.run(
                self._image_tag,
                detach=True,
                remove=False,   # we remove explicitly in stop_container
                environment=env,
                ports={"8080/tcp": port},
                name=f"devagent-test-{int(time.time())}",
            )
            self._container_id = container.id[:12]
            logger.info(f"[{self.name}] Container started: {self._container_id}")
            return {
                "container_id": self._container_id,
                "port":         port,
                "status":       "running",
            }

        except Exception as e:
            raise ToolError(f"Failed to start container: {e}")

    def _check_health(self, endpoint: str, port: int, timeout: int) -> dict:
        if not self._container_id:
            raise ToolError("No container running. Call start_container first.")

        import urllib.request
        import urllib.error

        url      = f"http://localhost:{port}{endpoint}"
        deadline = time.time() + timeout
        attempt  = 0

        logger.info(
            f"[{self.name}] Polling {url} "
            f"(timeout={timeout}s, interval={POLL_INTERVAL}s)"
        )

        while time.time() < deadline:
            attempt += 1
            try:
                resp = urllib.request.urlopen(url, timeout=3)
                if resp.status == 200:
                    elapsed = round(time.time() - (deadline - timeout), 1)
                    logger.info(
                        f"[{self.name}] Health check passed "
                        f"(attempt {attempt}, {elapsed}s)"
                    )
                    return {
                        "healthy":  True,
                        "url":      url,
                        "attempts": attempt,
                        "elapsed":  elapsed,
                        "status":   resp.status,
                    }
            except Exception:
                pass

            time.sleep(POLL_INTERVAL)

        # Timed out
        self.memory.add_finding(Finding.create(
            agent=self.name,
            severity=Severity.HIGH,
            title=f"Health check timed out after {timeout}s",
            description=(
                f"Container {self._container_id} did not respond "
                f"healthy on {url} within {timeout}s ({attempt} attempts)."
            ),
            suggestion=(
                "Check container logs for startup errors. "
                "Increase startup_timeout_seconds in docker.md if the app needs more time."
            ),
            source="docker:health-timeout",
        ))
        return {
            "healthy":  False,
            "url":      url,
            "attempts": attempt,
            "timeout":  timeout,
        }

    def _read_logs(self, tail: int) -> dict:
        if not self._container_id:
            return {"logs": "", "message": "No container running"}

        client = self._docker_client()
        try:
            container = client.containers.get(self._container_id)
            logs      = container.logs(tail=tail, timestamps=False).decode("utf-8", errors="ignore")
            return {"logs": logs, "lines": logs.count("\n")}
        except Exception as e:
            return {"logs": "", "error": str(e)}

    def _stop_container(self) -> dict:
        if not self._container_id:
            return {"stopped": False, "reason": "No container to stop"}

        client = self._docker_client()
        try:
            container = client.containers.get(self._container_id)
            container.stop(timeout=5)
            container.remove(force=True)
            logger.info(f"[{self.name}] Container {self._container_id} stopped and removed")
            cid = self._container_id
            self._container_id = None
            return {"stopped": True, "container_id": cid}
        except Exception as e:
            logger.warning(f"[{self.name}] Error stopping container: {e}")
            return {"stopped": False, "error": str(e)}

    def _post_report(self, passed: bool, skipped: bool, summary: str) -> dict:
        self.memory.set("docker_passed",  passed)
        self.memory.set("docker_skipped", skipped)
        self.memory.set("docker_summary", summary)

        if skipped:
            verdict = "[SKIPPED]"
        elif passed:
            verdict = "[ok] PASSED"
        else:
            verdict = "[FAILED]"

        build_time = self.memory.get("docker_build_time", "n/a")
        image_size = self.memory.get("docker_image_size", "n/a")

        report = (
            f"\n=== Docker Check {verdict} ===\n"
            f"Build time: {build_time}s | Image size: {image_size}MB\n"
            f"{summary}\n"
            f"{'=' * 30}\n"
        )
        print(report)
        logger.info(f"[{self.name}] Docker check complete - passed={passed}, skipped={skipped}")

        return {"passed": passed, "skipped": skipped, "summary": summary}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _docker_client(self):
        try:
            import docker
            return docker.from_env()
        except ImportError:
            raise ToolError("docker SDK not installed. Run: pip install docker")
        except Exception as e:
            raise ToolError(f"Cannot connect to Docker: {e}")

    def _handle_docker_unavailable(self, reason: str) -> None:
        logger.warning(f"[{self.name}] Docker not available: {reason}")
        self.memory.add_finding(Finding.create(
            agent=self.name,
            severity=Severity.LOW,
            title="Docker check skipped",
            description=reason,
            suggestion="Install Docker Desktop and ensure daemon is running",
            source="docker:unavailable",
        ))
        self.memory.set("docker_skipped", True)
        self.memory.set("docker_passed",  True)   # skipped = not failed
