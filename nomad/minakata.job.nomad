job "minakata" {
  datacenters = ["dc1"]
  type        = "service"

  group "minakata-api" {
    count = 2

    network {
      port "http" {
        to = 8000
      }
    }

    task "minakata-api" {
      driver = "docker"

      config {
        image = "${DOCKER_IMAGE}"
        ports = ["http"]
      }

      resources {
        cpu    = 256
        memory = 256
      }

      service {
        name = "minakata"
        port = "http"

        check {
          type     = "http"
          path     = "/health"
          interval = "10s"
          timeout  = "3s"
        }
      }
    }
  }
}