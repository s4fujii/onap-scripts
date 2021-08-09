import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

import kubernetes.client
import yaml
from kubernetes import config, client

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

READY_CHECK_INTERVAL = 30

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.DEBUG)


class HelmError(Exception):
    """Helm に関するエラー"""
    pass


class DeploymentDescriptor:
    """デプロイ定義ファイルのデータモデル"""
    def __init__(self):
        self.namespace = 'onap'
        self.release_name = 'dev'
        self.base_override = 'override.yaml'
        self.readiness_timeout = 300
        self.master_password = 'pw'
        self.deploy_order = []

    @staticmethod
    def from_file(path: str):
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
        desc = DeploymentDescriptor()
        desc.namespace = str(data['namespace'])
        desc.release_name = str(data['release_name'])
        desc.base_override = os.path.expanduser(str(data['base_override']))
        desc.readiness_timeout = int(data['readiness_timeout'])
        desc.master_password = str(data['master_password'])
        desc.deploy_order = data['deploy_order']
        return desc

    def __str__(self):
        return 'DeploymentDescriptor(namespace=%s, release_name=%s, base_override=%s, ' \
               'timeout=%d, password=%s, deploy_order=%s)' % (
                   self.namespace, self.release_name, self.base_override, self.readiness_timeout, self.master_password,
                   self.deploy_order)


def setup_logging() -> None:
    """ログ出力の設定を行う。標準出力とファイルの両方にログを出力する。
    """
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
    parser.add_argument('--release', '-r', help='release name', required=False)
    parser.add_argument('subcharts', help='subchart names to deploy', nargs='*')
    parser.add_argument('--skip-deploy', '-s', help='not execute helm deploy', action='store_true', default=False)
    parser.add_argument('--config', '-c',
                        help='path to kubeconfig file. ~/.kube/config or KUBECONFIG will be used if not specified.',
                        metavar='FILE')
    parser.add_argument('--namespace', '-n', help='kubernetes namespace where ONAP is deployed.',
                        default=DEFAULT_NAMESPACE)
    parser.add_argument('--descriptor', '-d', help='deployment descriptor file.', metavar='FILE')
    return parser.parse_args()


def wait_for_ready(api: kubernetes.client.AppsV1Api, ns: str, release_name: str) -> None:
    """指定されたHelmのReleaseで生成されたDeploymentやStatefulSetなどのオブジェクトが全てReadyになるまで待機する。

    :param any api: Kubernetes API オブジェクト
    :param str ns: 対象のNamespace
    :param str release_name: Release Name
    """
    _logger.info("Looking up resources for %s", release_name)

    # Deployments を取得し、annotation の release_name が一致するものを抽出
    deployments = api.list_namespaced_deployment(ns)
    watch_deps = [x for x in deployments.items if
                  'meta.helm.sh/release-name' in x.metadata.annotations and
                  x.metadata.annotations['meta.helm.sh/release-name'] == release_name]
    _logger.info("Deployments: %s", [x.metadata.name for x in watch_deps])

    # StatefulSets を取得し、annotation の release_name が一致するものを抽出
    statefulsets = api.list_namespaced_stateful_set(ns)
    watch_statesets = [x for x in statefulsets.items if
                       'meta.helm.sh/release-name' in x.metadata.annotations and
                       x.metadata.annotations['meta.helm.sh/release-name'] == release_name]
    _logger.info("StatefulSets: %s", [x.metadata.name for x in watch_statesets])

    # それぞれの deployment が replica 数分 ready になるのを待つ
    for deployment in watch_deps:
        name = deployment.metadata.name
        desired = deployment.spec.replicas or 0
        latest_status = api.read_namespaced_deployment_status(name, ns)
        ready = latest_status.status.ready_replicas or 0
        while desired != ready:
            _logger.info('Waiting for Deployment %s to be ready (desired=%d, ready=%d)...', name, desired, ready)
            time.sleep(READY_CHECK_INTERVAL)
            # Get the latest status
            latest_status = api.read_namespaced_deployment_status(name, ns)
            ready = latest_status.status.ready_replicas or 0
        _logger.info('%s / Deployment %s is ready', release_name, name)

    # それぞれの statefulset が replica 数分 ready になるのを待つ
    for statefulset in watch_statesets:
        name = statefulset.metadata.name
        desired = statefulset.spec.replicas or 0
        latest_status = api.read_namespaced_stateful_set_status(name, ns)
        ready = latest_status.status.ready_replicas or 0
        while desired != ready:
            _logger.info('Waiting for StatefulSet %s to be ready (desired=%d, ready=%d)...', name, desired, ready)
            time.sleep(READY_CHECK_INTERVAL)
            # Get the latest status
            latest_status = api.read_namespaced_stateful_set_status(name, ns)
            ready = latest_status.status.ready_replicas or 0
        _logger.info('%s / StatefulSet %s is ready', release_name, name)


def deploy_subchart(desc: DeploymentDescriptor, subchart: str) -> None:
    """Helm で指定された subchart をインストールする。
    """
    subchart_release = '%s-%s' % (desc.release_name, subchart)
    helm_cmd = ['helm', 'deploy', subchart_release, 'local/onap', '--namespace', desc.namespace, '-f',
                desc.base_override,
                '--set', 'global.masterPassword=%s' % desc.master_password,
                '--set', '%s.enabled=true' % subchart, '--verbose', '--debug']
    _logger.info('Running: %s', ' '.join(helm_cmd))
    try:
        with open('helm-deploy.log', 'a') as f:
            helm_result = subprocess.run(helm_cmd, stdout=f)
        # helm deploy の終了コードが 0 以外の場合は処理中止
        _logger.info('Return code: %d', helm_result.returncode)
        if helm_result.returncode != 0:
            _logger.error('helm returned an error. aborted.')
            sys.exit(helm_result.returncode)
    except FileNotFoundError as e:
        _logger.exception('helm not found. Please install helm and try again. %s', e)
        sys.exit(1)


def list_releases(namespace: str) -> list:
    """指定された名前空間でインストールされている Helm リリース一覧を取得する。

    :param namespace: 名前空間
    :type namespace: str
    :return: リリース一覧のリスト
    :rtype: list
    """
    helm_cmd = ['helm', 'list', '-n', namespace]
    _logger.info('Running: %s', ' '.join(helm_cmd))
    try:
        # note: capture_output is available on python 3.7 or later
        helm_result = subprocess.run(helm_cmd, stdout=subprocess.PIPE)
        _logger.info('Return code: %d', helm_result.returncode)
        if helm_result.returncode != 0:
            _logger.error('helm returned an error (code=%d). aborted.', helm_result.returncode)
            raise HelmError
        output_lines = helm_result.stdout.decode().splitlines()
        result = []
        # We expect a header line. so remove it by output_lines[1:]
        for line in output_lines[1:]:
            row = re.split(r'\s+', line)
            result.append(row)
        return result
    except FileNotFoundError as e:
        _logger.exception('helm not found. Please install helm and try again. %s', e)
        raise


def main():
    # ログ初期設定
    setup_logging()
    dd = DeploymentDescriptor()

    # コマンドライン引数処理
    args = parse_cmdline_args()
    _logger.debug('Parsed arguments: %s', args)

    # 引数で指定された場合はその値を使い、そうでない場合はデフォルト値を使用
    config_candidate = [args.config] if args.config else DEFAULT_KUBECONFIG

    # config_candidate リストにあるファイルのどれかが読み込めるかチェックする
    for kubepath in config_candidate:
        if os.path.exists(kubepath):
            _logger.info('Using kube config file: %s', kubepath)
            config.load_kube_config(kubepath)
            break
    else:
        _logger.error('Cannot read any of kubeconfig file(s): %s', config_candidate)
        sys.exit(1)

    # 引数でデスクリプタが指定されている場合は読み込む
    if args.descriptor:
        try:
            desc = DeploymentDescriptor.from_file(args.descriptor)
        except OSError as e:
            _logger.error('Failed to load descriptor file.', exc_info=e)
            sys.exit(1)
        except KeyError as e:
            _logger.error('Failed to parse descriptor file.', exc_info=e)
            sys.exit(1)
    else:
        # デスクリプタ・ファイルが指定されていない場合はコマンドライン引数に従う
        desc = DeploymentDescriptor()
        desc.deploy_order.append(args.subcharts)
        desc.namespace = args.namespace
        _logger.info('Using namespace: %s', desc.namespace)
        desc.release_name = args.release
    # _logger.debug('descriptor: %s', json.dumps(desc))
    _logger.debug('descriptor: %s', desc)

    # 現在インストール済みのリリース一覧を取得し、名前を - で分割してサブチャート名を得る
    helm_out = list_releases(desc.namespace)
    installed_subcharts = []
    for row in helm_out:
        s = str(row[0]).split('-', maxsplit=1)
        if len(s) >= 2:
            installed_subcharts.append(s[1])
    _logger.debug('installed subcharts: %s', installed_subcharts)

    # Kubernetes apps v1 クライアント作成
    apps_v1 = client.AppsV1Api()

    stage = 0
    for subcharts in desc.deploy_order:
        stage = stage + 1
        _logger.debug('Stage %d: subcharts = %s', stage, subcharts)

        # Subchart をインストールする。skip_deploy が指定されている場合はスキップする。
        if not args.skip_deploy:
            for subchart in subcharts:
                # 既にインストールされている場合はスキップ
                if subchart in installed_subcharts:
                    _logger.info('Deploying %s has been skipped because already installed', subchart)
                    continue
                _logger.info('Installing subchart %s ...', subchart)
                deploy_subchart(desc, subchart)
        else:
            _logger.info('Deploying subcharts has been skipped because skip_deploy is true')

        # Deployments, StatefulSets が全て Ready になるまで待つ
        for subchart in subcharts:
            subchart_release_name = desc.release_name + '-' + subchart
            wait_for_ready(apps_v1, desc.namespace, subchart_release_name)


if __name__ == '__main__':
    main()
