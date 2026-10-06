import logging
import secrets
import time
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from hivemind_worker import __version__
from hivemind_worker.config import WorkerConfig
from hivemind_worker.models import (
    CancelResponse,
    DispatchRequest,
    DispatchResponse,
    HealthResponse,
    StatusResponse,
)
from hivemind_worker.runner import TaskRunner
from hivemind_worker.sysinfo import get_system_health

# Configure logger for worker daemon
logger = logging.getLogger("hivemind_worker")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


def create_app(config: Optional[WorkerConfig] = None) -> FastAPI:
    """Factory function to build configured FastAPI application instance."""
    app_config = config or WorkerConfig.load()
    workspace_root = Path(app_config.workspace_root)
    runner = TaskRunner(workspace_root)

    app = FastAPI(
        title="HiveMind Worker Daemon",
        description="Headless task execution node for distributed HiveMind agent swarm",
        version=__version__,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # HTTP Request / Response logging middleware for journalctl visibility
    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        start_time = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path

        logger.info(f"--> [{client_ip}] {method} {path}")
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000
            status_code = response.status_code
            
            log_level = logger.info if status_code < 400 else (logger.warning if status_code < 500 else logger.error)
            log_level(f"<-- [{client_ip}] {method} {path} -> {status_code} ({duration_ms:.1f}ms)")
            return response
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"<-- [{client_ip}] {method} {path} -> 500 Exception: {exc} ({duration_ms:.1f}ms)")
            raise

    # Store in app state
    app.state.config = app_config
    app.state.runner = runner

    def verify_auth(request: Request, authorization: Optional[str] = Header(None)):
        fleet_key = app_config.fleet_key
        client_ip = request.client.host if request.client else "unknown"
        if not fleet_key:
            return

        if not authorization:
            logger.warning(f"Unauthorized access from [{client_ip}]: Missing Authorization header on {request.url.path}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
                headers={"WWW-Authenticate": "Bearer"},
            )

        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning(f"Unauthorized access from [{client_ip}]: Malformed Authorization header")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization scheme. Use 'Bearer <key>'",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = parts[1]
        if not secrets.compare_digest(token, fleet_key):
            logger.warning(f"Unauthorized access from [{client_ip}]: Invalid fleet API key supplied")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid fleet API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/", tags=["Info"])
    def root_info():
        return {
            "name": "HiveMind Worker Daemon",
            "version": __version__,
            "status": "online",
        }

    @app.get("/health", response_model=HealthResponse, tags=["Diagnostics"], dependencies=[Depends(verify_auth)])
    def health():
        """Instant hardware and capability probe endpoint."""
        active = runner.get_active_run_ids()
        return get_system_health(active_runs=active)

    @app.post("/dispatch", response_model=DispatchResponse, tags=["Execution"], dependencies=[Depends(verify_auth)])
    def dispatch(req: DispatchRequest):
        """Dispatch a task to run headless in the background."""
        st = runner.dispatch(req)
        return DispatchResponse(
            run_id=req.run_id,
            status=st.status,
            message=f"Task {req.run_id} accepted and queued for execution."
        )

    @app.get("/status/{run_id}", response_model=StatusResponse, tags=["Execution"], dependencies=[Depends(verify_auth)])
    def get_status(run_id: str):
        """Retrieve live status, branch, and test logs of a task."""
        st = runner.get_run_status(run_id)
        if not st:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        return st

    @app.delete("/cancel/{run_id}", response_model=CancelResponse, tags=["Execution"], dependencies=[Depends(verify_auth)])
    def cancel_task(run_id: str):
        """Abort a running task."""
        success = runner.cancel_run(run_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Active run '{run_id}' not found to cancel.")
        return CancelResponse(
            run_id=run_id,
            cancelled=True,
            message=f"Task '{run_id}' successfully cancelled."
        )

    @app.get("/runs", response_model=List[StatusResponse], tags=["Execution"], dependencies=[Depends(verify_auth)])
    def list_all_runs():
        """List all historical and active task runs on this worker."""
        return runner.list_runs()

    return app
