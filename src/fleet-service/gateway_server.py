from monad_fleet_service.core import *  # noqa: F401,F403
from monad_fleet_service.main import serve
from monad_fleet_service.servicer_v1 import FleetManagerServicer
from monad_fleet_service.servicer_v2 import FleetManagerServicerV2


if __name__ == "__main__":
    serve()
