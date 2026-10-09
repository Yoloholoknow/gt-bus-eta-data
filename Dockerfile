FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir requests
COPY . .

# Exit when either collector dies so Fly restarts the machine.
CMD ["bash", "-c", "python -m gtbus.collect.vehicle_heading & python -m gtbus.collect.stop_visits & wait -n"]
