job "minakata" {
  datacenters = ["dc1"]
  type        = "service"

  group "minakata-api" {
    count = 2

    network {
      port "http" {
        to = 7070
      }
    }

    task "minakata-api" {
      driver = "docker"

      config {
        image = "${DOCKER_IMAGE}"
        ports = ["http"]
      }

      template {
                        data        = <<EOF
                {{ with nomadVar "nomad/jobs/minakata" }}
                LINE_ACCESS_TOKEN={{ .LINE_ACCESS_TOKEN }}
                LINE_USER_ID={{ .LINE_USER_ID }}
                {{ end }}
                NOTIFY_CITY=Kamakura
                EOF
        destination = "secrets/minakata.env"
        env         = true
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