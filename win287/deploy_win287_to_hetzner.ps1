param(
  [string]$HostName = "87.99.151.70",
  [string]$UserName = "root",
  [string]$KeyPath = "$env:USERPROFILE\.ssh\hetzner_sniper",
  [string]$RemoteDir = "/root/piggy"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = "$UserName@$HostName"

$RootFiles = @(
  "_launch_v287_oneentry_smoke.sh",
  "pgg2_v287_selected_band_live_smoke.py",
  "v287_authority_replay.py",
  "birth_first_sniper.py",
  "pgg2_direct_pump.py",
  "pgg2_live_raptor.py",
  "pgg2_v74_sender_adapter.py",
  "pgg2_v75_sender_tx_builder.py",
  "pgg2_v285_grpc_buy_train_continuation_no_send.py",
  "pgg2_v129_sof_stagea_live_bundle.py",
  "pgg2_v108_bundle_builder.py",
  "pgg2_v108_bundle_profit_model.py",
  "pgg2_v108_external_tx_decoder.py",
  "pgg2_v108_jito_bundle_sender.py",
  "pgg2_v109_no_send_live_bundle_validation.py",
  "pgg2_v129_sof_no_send_bundle_validation.py"
)

$ProtoFiles = @(
  "geyser_pb2.py",
  "geyser_pb2_grpc.py",
  "solana_storage_pb2.py",
  "solana_storage_pb2_grpc.py",
  "geyser.proto",
  "solana-storage.proto"
)

ssh -i $KeyPath $Target "mkdir -p $RemoteDir/yellowstone_proto $RemoteDir/logs"

foreach ($File in $RootFiles) {
  scp -q -i $KeyPath (Join-Path $Here $File) "${Target}:$RemoteDir/"
}

foreach ($File in $ProtoFiles) {
  scp -q -i $KeyPath (Join-Path $Here "yellowstone_proto\$File") "${Target}:$RemoteDir/yellowstone_proto/"
}

ssh -i $KeyPath $Target "cd $RemoteDir && chmod +x _launch_v287_oneentry_smoke.sh && bash -n _launch_v287_oneentry_smoke.sh && /root/piggy/venv/bin/python -m py_compile pgg2_v287_selected_band_live_smoke.py v287_authority_replay.py pgg2_v285_grpc_buy_train_continuation_no_send.py pgg2_direct_pump.py pgg2_v74_sender_adapter.py pgg2_v75_sender_tx_builder.py"

Write-Host "WIN287_DEPLOY_OK ${Target}:$RemoteDir"
