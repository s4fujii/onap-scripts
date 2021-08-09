from kubernetes import config, client

import os
import sys
import argparse
import json
import time
import subprocess
import pathlib
import yaml
import logging

# ホームディレクトリの取得
if os.name == 'nt':
    # Windows の場合は USERPROFILE 環境変数を使用
    home_dir = os.environ.get('USERPROFILE', '')
else:
    home_dir = os.environ.get('HOME', '')

# --config オプションが指定されなかった時にデフォルトで検索する kubeconfig ファイルのパス
# KUBECONFIG 環境変数が空であった場合は取り除くために filter を使用
DEFAULT_KUBECONFIG = list(filter(None, [
    os.path.join(os.path.curdir, 'kubeconfig'),
    os.environ.get('KUBECONFIG', ''),
    os.path.join(home_dir, '.kube', 'config')
]))

# --namespace オプションが指定されなかった時に使用する namespace 名
DEFAULT_NAMESPACE = 'onap'

READY_CHECK_INTERVAL = 15

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.DEBUG)


def setup_logging() -> None:
    '''ログ出力の設定を行う。標準出力とファイルの両方にログを出力する。
    '''
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s | %(levelname)5s | %(module)s | %(funcName)s | %(message)s')
    # 標準出力
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    # ファイル出力
    fhandler = logging.FileHandler('deploy.log')
    fhandler.setFormatter(formatter)
    root.addHandler(fhandler)


def parse_cmdline_args() -> argparse.Namespace:
    """コマンドライン引数を処理する。

    :return: プログラムに渡された引数の情報
    """
    parser = argparse.ArgumentParser()
    parser.description = 'ONAP deploy script'
    parser.add_argument('release', help='release name')
    parser.add_argument('subcharts', help='subchart names to deploy', nargs='*')
    parser.add_argument('--skip-deploy', '-s', help='not execute helm deploy', action='store_true', default=False)
    parser.add_argument('--config', '-c',
                        help='path to kubeconfig file. ~/.kube/config or KUBECONFIG will be used if not specified.',
                        metavar='FILE')
    parser.add_argument('--namespace', '-n', help='kubernetes namespace where ONAP is deployed.',
                        default=DEFAULT_NAMESPACE)
    return parser.parse_args()


def wait_for_ready(api: any, ns: str, release_name: str) -> None:
    """指定されたHelmのReleaseで生成されたDeploymentやStatefulSetなどのオブジェクトが全てReadyになるまで待機する。

    :param any api: Kubernetes API オブジェクト
    :param str ns: 対象のNamespace
    :param str release_name: Release Name
    """
    _logger.info("Looking up resources for %s", release_name)

    # Deployments を取得し、annotation の release_name が一致するものを抽出
    deployments = apps_v1.list_namespaced_deployment(ns)
    watch_deps = [x for x in deployments.items if
                  'meta.helm.sh/release-name' in x.metadata.annotations and
                  x.metadata.annotations['meta.helm.sh/release-name'] == release_name]
    _logger.info("Deployments: %s", [x.metadata.name for x in watch_deps])

    # StatefulSets を取得し、annotation の release_name が一致するものを抽出
    statefulsets = apps_v1.list_namespaced_stateful_set(ns)
    watch_statesets = [x for x in statefulsets.items if
                       'meta.helm.sh/release-name' in x.metadata.annotations and
                       x.metadata.annotations['meta.helm.sh/release-name'] == release_name]
    _logger.info("StatefulSets: %s", [x.metadata.name for x in watch_statesets])

    # それぞれの deployment が replica 数分 ready になるのを待つ
    for deployment in watch_deps:
        name = deployment.metadata.name
        desired = deployment.spec.replicas or 0
        latest_status = apps_v1.read_namespaced_deployment_status(name, ns)
        ready = latest_status.status.ready_replicas or 0
        while desired != ready:
            _logger.info('Waiting for Deployment %s to be ready (desired=%d, ready=%d)...', name, desired, ready)
            time.sleep(READY_CHECK_INTERVAL)
            # Get the latest status
            latest_status = apps_v1.read_namespaced_deployment_status(name, ns)
            ready = latest_status.status.ready_replicas or 0
        _logger.info('%s / Deployment %s is ready', release_name, name)

    # それぞれの statefulset が replica 数分 ready になるのを待つ
    for statefulset in watch_statesets:
        name = statefulset.metadata.name
        desired = statefulset.spec.replicas or 0
        latest_status = apps_v1.read_namespaced_stateful_set_status(name, ns)
        ready = latest_status.status.ready_replicas or 0
        while desired != ready:
            _logger.info('Waiting for StatefulSet %s to be ready (desired=%d, ready=%d)...', name, desired, ready)
            time.sleep(READY_CHECK_INTERVAL)
            # Get the latest status
            latest_status = apps_v1.read_namespaced_stateful_set_status(name, ns)
            ready = latest_status.status.ready_replicas or 0
        _logger.info('%s / StatefulSet %s is ready', release_name, name)


if __name__ == '__main__':
    # ログ初期設定
    setup_logging()

    # コマンドライン引数処理
    args = parse_cmdline_args()
    _logger.debug('Parsed arguments: %s', args)

    # 引数で指定された場合はその値を使い、そうでない場合はデフォルト値を使用
    config_candidate = [args.config] if args.config else DEFAULT_KUBECONFIG
    namespace = args.namespace
    _logger.info('Using namespace: %s', namespace)

    # config_candidate リストにあるファイルのどれかが読み込めるかチェックする
    for kubepath in config_candidate:
        if os.path.exists(kubepath):
            _logger.info('Using kube config file: %s', kubepath)
            config.load_kube_config(kubepath)
            break
    else:
        _logger.error('Cannot read any of kubeconfig file(s): %s', config_candidate)
        sys.exit(1)

    # Kubernetes クライアント作成
    apps_v1 = client.AppsV1Api()

    # release名とsubchartリストからsubchartのリリース名をリスト化
    subcharts = [args.release + '-' + x for x in args.subcharts]

    # helm deploy 実行
    if not args.skip_deploy:
        override_path = os.path.expanduser('~/onap/oom-override/override.yaml')
        if not os.path.exists(override_path):
            _logger.error('Values file not found: %s', override_path)
            sys.exit(1)
        master_password = 'password'
        for subchart in subcharts:
            helm_cmd = ['helm', 'deploy', subchart, 'local/onap', '--namespace', namespace, '-f', override_path,
                        '--set', 'global.masterPassword=%s' % master_password, '--verbose', '--debug']
            _logger.info('Running: %s', ' '.join(helm_cmd))
            try:
                helm_result = subprocess.run(helm_cmd)
                # helm deploy の終了コードが 0 以外の場合は処理中止
                _logger.info('Return code: %d', helm_result.returncode)
                if helm_result.returncode != 0:
                    _logger.error('helm returned an error. aborted.')
                    sys.exit(helm_result.returncode)
            except FileNotFoundError as e:
                _logger.exception('helm not found. Please install helm and try again. %s', e)
                sys.exit(1)

    for subchart in subcharts:
        wait_for_ready(apps_v1, namespace, subchart)

    # deployments = apps_v1.list_namespaced_deployment(namespace)
    # for deployment in deployments.items:
    #     ready_replicas = deployment.status.ready_replicas or 0
    #     total_replicas = deployment.status.replicas or 0
    #     print('name=%s, chart=%s, %d/%d' % (
    #         deployment.metadata.name, deployment.metadata.annotations['meta.helm.sh/release-name'], ready_replicas,
    #         total_replicas))
