job "minakata" {
  datacenters = ["dc1"]
  type        = "service"

  group "web" {
    count = 1

    network {
      port "http" {
        static = 8080
      }
    }

    task "server" {
      driver = "docker"

      config {
        # ここをECRのイメージURIに書き換えてください
        image = "871950640338.ecr.ap-northeast-1.amazonaws.com/minakata:latest"
        ports = ["http"]
      }

      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
