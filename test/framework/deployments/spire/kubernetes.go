package spire

import (
	"context"

	"github.com/gruntwork-io/terratest/modules/helm"
	"github.com/gruntwork-io/terratest/modules/k8s"
	"github.com/gruntwork-io/terratest/modules/retry"
	"github.com/pkg/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/kumahq/kuma/v2/test/framework"
)

type k8sDeployment struct {
	namespace      string
	name           string
	trustDomain    string
	kubectlVersion string
}

func (u *k8sDeployment) GetIP() (string, error) {
	panic("not implemented")
}

var _ framework.Deployment = &k8sDeployment{}

func (t *k8sDeployment) Name() string {
	return DeploymentName
}

func (t *k8sDeployment) Deploy(cluster framework.Cluster) error {
	var err error
	if t.namespace == "" {
		t.namespace = "spire"
	}

	opts := helm.Options{
		KubectlOptions: cluster.GetKubectlOptions(t.namespace),
	}
	// install crds
	_, err = helm.RunHelmCommandAndGetStdOutE(cluster.GetTesting(), &opts, "install", "spire-crds",
		"--namespace", t.namespace,
		"--repo", "https://spiffe.github.io/helm-charts-hardened/",
		"spire-crds",
	)
	if err != nil {
		return err
	}

	_, err = helm.RunHelmCommandAndGetStdOutE(cluster.GetTesting(), &opts, "install", "spire",
		"--namespace", t.namespace,
		"--repo", "https://spiffe.github.io/helm-charts-hardened/",
		"--set", "global.spire.trustDomain="+t.trustDomain,
		"--set", "global.spire.tools.kubectl.tag="+t.kubectlVersion,
		"spire",
	)
	if err != nil {
		return err
	}

	err = t.isPodReady(cluster, "app.kubernetes.io/name=agent")
	if err != nil {
		return err
	}
	err = t.isPodReady(cluster, "app.kubernetes.io/name=server")
	if err != nil {
		return err
	}
	err = t.isPodReady(cluster, "app.kubernetes.io/name=spiffe-csi-driver")
	if err != nil {
		return err
	}
	err = t.isPodReady(cluster, "app.kubernetes.io/name=spiffe-oidc-discovery-provider")
	if err != nil {
		return err
	}
	// Wait for the spire-controller-manager webhook service to have endpoints before returning.
	// The controller-manager may run as a sidecar in spire-server-0 or as a separate deployment
	// depending on the chart version, so we wait for the service endpoints directly rather than
	// waiting for a pod with a specific label.
	_, err = retry.DoWithRetryE(cluster.GetTesting(),
		"wait for spire-controller-manager-webhook to have endpoints",
		framework.DefaultRetries*3, // spire is fetched from the internet. Increase the timeout to prevent long downloads of images.
		framework.DefaultTimeout,
		func() (string, error) {
			client, err := k8s.GetKubernetesClientFromOptionsE(cluster.GetTesting(), cluster.GetKubectlOptions(t.namespace))
			if err != nil {
				return "", err
			}
			endpoints, err := client.CoreV1().Endpoints(t.namespace).Get(context.Background(), "spire-controller-manager-webhook", metav1.GetOptions{})
			if err != nil {
				return "", err
			}
			for _, subset := range endpoints.Subsets {
				if len(subset.Addresses) > 0 {
					return "", nil
				}
			}
			return "", errors.Errorf("service %q has no ready endpoints yet", "spire-controller-manager-webhook")
		})
	if err != nil {
		return err
	}

	return nil
}

func (t *k8sDeployment) isPodReady(cluster framework.Cluster, selector string) error {
	err := k8s.WaitUntilNumPodsCreatedE(cluster.GetTesting(),
		cluster.GetKubectlOptions(t.namespace),
		metav1.ListOptions{
			LabelSelector: selector,
		},
		1,
		framework.DefaultRetries*3, // spire is fetched from the internet. Increase the timeout to prevent long downloads of images.
		framework.DefaultTimeout)
	if err != nil {
		return err
	}

	pods := k8s.ListPods(cluster.GetTesting(),
		cluster.GetKubectlOptions(t.namespace),
		metav1.ListOptions{
			LabelSelector: selector,
		},
	)
	if len(pods) == 0 {
		return errors.Errorf("no pods found with selector %q in namespace %q", selector, t.namespace)
	}

	for _, pod := range pods {
		if err := k8s.WaitUntilPodAvailableE(cluster.GetTesting(),
			cluster.GetKubectlOptions(t.namespace),
			pod.Name,
			framework.DefaultRetries*3, // spire is fetched from the internet. Increase the timeout to prevent long downloads of images.
			framework.DefaultTimeout); err != nil {
			return err
		}
	}

	return nil
}

func (t *k8sDeployment) Delete(cluster framework.Cluster) error {
	return cluster.(*framework.K8sCluster).TriggerDeleteNamespace(t.namespace)
}
