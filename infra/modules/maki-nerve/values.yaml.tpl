config:
  jetstream:
    enabled: true
    fileStore:
      pvc:
        size: ${jetstream_storage_size}
        storageClassName: ${storage_class}
  merge:
    authorization:
      token: "<< $TOKEN >>"
%{ if length(nats_cluster_routes) > 0 ~}
    cluster:
      name: maki-nats
      routes:
%{ for route in nats_cluster_routes ~}
        - ${route}
%{ endfor ~}
%{ endif ~}
container:
  env:
    TOKEN:
      valueFrom:
        secretKeyRef:
          key: token
          name: maki-nats-auth
