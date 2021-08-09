from kubernetes import config, client
import os
import sys
import argparse
import json

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

# --state オプションが指定されなかった時に使用する state ファイルのパス
DEFAULT_STATE = os.path.join(home_dir, '.onap', 'last-state')

# コマンドライン引数パーサの準備
parser = argparse.ArgumentParser()
parser.description = 'ONAP start/stop script'
parser.add_argument('action', help='action to perform', choices=['start', 'stop'])
parser.add_argument('--config', '-c', help='path to kubeconfig file. ~/.kube/config or KUBECONFIG will be used if not specified.', metavar='FILE')
parser.add_argument('--state', '-s', help='path to last state file (must be writable).', default=DEFAULT_STATE, metavar='FILE')
parser.add_argument('--namespace', '-n', help='kubernetes namespace where ONAP is deployed.', default=DEFAULT_NAMESPACE)
parser.add_argument('--force', '-f', action='store_true', help='force to do even if the specified action is the same as last time')

if __name__ == '__main__':
    # コマンドライン引数処理
    args = parser.parse_args()

    # 引数で指定された場合はその値を使い、そうでない場合はデフォルト値を使用
    config_candidate = [args.config] if args.config else DEFAULT_KUBECONFIG
    state_file = args.state
    namespace = args.namespace
    action = args.action

    # config_candidate リストにあるファイルのどれかが読み込めるかチェックする
    for kubepath in config_candidate:
        if os.path.exists(kubepath):
            print('info: using kube config file: ' + kubepath)
            config.load_kube_config(kubepath)
            break
    else:
        print('error: cannot read any of kubeconfig file(s): %s' % config_candidate)
        sys.exit(1)

    print('info: using state file: ' + state_file)
    print('info: using namespace: ' + namespace)

    # APIオブジェクト生成
    # v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    # state ファイルから前回の状態を読み出す
    last_action = 'none'
    try:
        with open(state_file, 'r') as f:
            state_data = json.load(f)
            # 最後に行われた action
            last_action = state_data['last_action']
            # 最後の各リソースの状態
            data = state_data['data']
    except Exception:
        # エラーの場合は処理をスキップ
        print('info: skip processing last state file')

    if action == 'start':
        if last_action == 'start':
            if args.force:
                print('warning: last action was "' + last_action + '" but do the same anyway')
            else:
                print('error: last action was "' + last_action + '". aborted.')
                sys.exit(3)
        if last_action == 'none' or data == None or len(data) == 0:
            print('error: no last state data. cannot restore to original state. aborted.')
            sys.exit(4)
        
        for item in data:
            kind = item['kind']
            if kind == 'deployment':
                scale = apps_v1.read_namespaced_deployment_scale(item['name'], item['namespace'])
                # replicas を復元する
                scale.spec.replicas = item['replicas']
                response = apps_v1.patch_namespaced_deployment_scale(item['name'], item['namespace'], scale)
            if kind == 'statefulSet':
                scale = apps_v1.read_namespaced_stateful_set_scale(item['name'], item['namespace'])
                # replicas を復元する
                scale.spec.replicas = item['replicas']
                response = apps_v1.patch_namespaced_stateful_set_scale(item['name'], item['namespace'], scale)

        # state ファイルの last_action を更新する
        last_action = action
        with open(state_file, 'w') as f:
            json.dump({'last_action': action, 'data': data}, f)

    elif action == 'stop':
        if last_action == 'stop':
            if args.force:
                print('warning: last action was "' + last_action + '" but do the same anyway')
            else:
                print('error: last action was "' + last_action + '". aborted.')
                sys.exit(3)
        deployments = apps_v1.list_namespaced_deployment(namespace)
        data = []
        # TODO: 内包表記に変える
        for deployment in deployments.items:
            print('name=%s, desired=%s, ready=%s' % (
                deployment.metadata.name, deployment.status.replicas, deployment.status.ready_replicas))
            if deployment.status.replicas == None:
                continue
            data.append({
                'namespace': namespace,
                'kind': 'deployment',
                'name': deployment.metadata.name,
                'replicas': deployment.status.replicas
            })

        stateful_sets = apps_v1.list_namespaced_stateful_set(namespace)
        for stateful_set in stateful_sets.items:
            print('name=%s, desired=%s, ready=%s' % (
                stateful_set.metadata.name, stateful_set.status.replicas, stateful_set.status.ready_replicas))
            if stateful_set.status.replicas == None:
                continue
            data.append({
                'namespace': namespace,
                'kind': 'statefulSet',
                'name': stateful_set.metadata.name,
                'replicas': stateful_set.status.replicas
            })

        # state_file を格納する親ディレクトリを作成する
        if os.path.dirname(state_file):
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, 'w') as f:
            json.dump({'last_action': action, 'data': data}, f)

        for item in data:
            if item['kind'] == 'deployment':
                scale = apps_v1.read_namespaced_deployment_scale(item['name'], item['namespace'])
                # print(scale)
                scale.spec.replicas = 0
                response = apps_v1.patch_namespaced_deployment_scale(item['name'], item['namespace'], scale)
                print(response)
            if item['kind'] == 'statefulSet':
                scale = apps_v1.read_namespaced_stateful_set_scale(item['name'], item['namespace'])
                # print(scale)
                scale.spec.replicas = 0
                response = apps_v1.patch_namespaced_stateful_set_scale(item['name'], item['namespace'], scale)
                print(response)
    else:
        print('error: unknown action: ' + action)
        sys.exit(2)
