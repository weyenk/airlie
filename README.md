# airlie

A Docker Compose-like tool for running multiple mlx models in tandem.

## Features

- Define model configurations in a single YAML file
- Run multiple models simultaneously with defined dependencies
- Automatic port mapping and service discovery
- Health checks for models
- Easy scaling of model instances

## Getting Started

1. Create a `models.yml` configuration file
2. Run `mlx-orchestrator up` to start all models
3. Access models via their configured ports

## Commands

- `mlx-orchestrator up` - Start all models defined in the configuration
- `mlx-orchestrator down` - Stop all running models
- `mlx-orchestrator status` - Show current status of all models
- `mlx-orchestrator logs` - View logs from all models