package context_test

import (
	"fmt"
	"net"
	"os"
	"path"
	"strings"
	"sync"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	kuma_cp "github.com/kumahq/kuma/v2/pkg/config/app/kuma-cp"
	config_core "github.com/kumahq/kuma/v2/pkg/config/core"
	config_store "github.com/kumahq/kuma/v2/pkg/config/core/resources/store"
	config_types "github.com/kumahq/kuma/v2/pkg/config/types"
	"github.com/kumahq/kuma/v2/pkg/core/resources/store"
	"github.com/kumahq/kuma/v2/pkg/kds/global"
	"github.com/kumahq/kuma/v2/pkg/kds/zone"
	"github.com/kumahq/kuma/v2/pkg/plugins/resources/memory"
	"github.com/kumahq/kuma/v2/pkg/test"
	"github.com/kumahq/kuma/v2/pkg/test/kds/setup"
	"github.com/kumahq/kuma/v2/pkg/test/matchers"
	test_store "github.com/kumahq/kuma/v2/pkg/test/store"
)

var _ = Describe("Full sync tests", func() {
	DescribeTable("Full sync tests", func(ctx SpecContext, folder string) {
		files, err := os.ReadDir(folder)
		Expect(err).ToNot(HaveOccurred())
		zones := make(map[string]store.ResourceStore)
		wg := sync.WaitGroup{}
		done := make(chan struct{})

		for _, file := range files {
			if strings.HasSuffix(file.Name(), ".input.yaml") {
				zoneName := strings.TrimSuffix(file.Name(), ".input.yaml")
				resourceStore := store.NewPaginationStore(memory.NewStore())
				fullPath := path.Join(folder, file.Name())
				Expect(test_store.LoadResourcesFromFile(ctx, resourceStore, fullPath)).To(Succeed())
				zones[zoneName] = resourceStore
			}
		}

		// Starts all the things

		globalStore := zones["global"]
		Expect(globalStore).ToNot(BeNil(), "global must be present")
		// start global
		cfg := kuma_cp.DefaultConfig()
		cfg.Store.Type = config_store.MemoryStore
		globalPort, err := test.GetFreePort()
		Expect(err).ToNot(HaveOccurred())
		cfg.Multizone.Global.KDS.GrpcPort = uint32(globalPort)
		cfg.Multizone.Global.KDS.TlsEnabled = false
		cfg.Multizone.Global.KDS.ZoneInsightFlushInterval = config_types.Duration{Duration: 100 * time.Millisecond}
		cfg.Mode = config_core.Global
		rt := setup.NewTestRuntime(ctx, cfg, globalStore)
		Expect(global.Setup(rt)).To(Succeed())
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer GinkgoRecover()
			Expect(rt.Start(done)).To(Succeed())
		}()
		// Wait for the global CP's gRPC server to be accepting connections before
		// starting zone CPs. Without this, zone mux clients can hit "connection
		// refused" on their first dial and trigger the resilient component's base
		// backoff (5s), consuming most of the 30s Eventually window for sync verification.
		Eventually(func() error {
			conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", globalPort), time.Second)
			if err != nil {
				return err
			}
			_ = conn.Close()
			return nil
		}, "10s", "50ms").Should(Succeed())
		// start zones
		for zoneName, zoneStore := range zones {
			if zoneName == "global" {
				continue
			}
			cfg := kuma_cp.DefaultConfig()
			cfg.Store.Type = config_store.MemoryStore
			cfg.Mode = config_core.Zone
			cfg.Multizone.Zone.Name = zoneName
			cfg.Multizone.Zone.GlobalAddress = fmt.Sprintf("grpc://127.0.0.1:%d", globalPort)
			cfg.Multizone.Global.KDS.ZoneInsightFlushInterval = config_types.Duration{Duration: 100 * time.Millisecond}
			rt := setup.NewTestRuntime(ctx, cfg, zoneStore)
			Expect(zone.Setup(rt)).To(Succeed())
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer GinkgoRecover()
				Expect(rt.Start(done)).To(Succeed())
			}()
		}

		// Wait for all stores to reach their expected final state before stopping CPs.
		// Checking against the golden files directly is the most reliable signal that
		// both sync directions (zone→global and global→zone) have fully completed.
		for zoneName, zoneStore := range zones {
			zoneName := zoneName
			zoneStore := zoneStore
			Eventually(func(g Gomega) {
				out, err := test_store.ExtractResources(ctx, zoneStore)
				g.Expect(err).ToNot(HaveOccurred())
				g.Expect(out).To(matchers.MatchGoldenEqual(folder, zoneName+".golden.yaml"))
			}, "30s", "100ms").Should(Succeed())
		}
		// All stores match their golden files; stop the CPs.
		close(done)
		wg.Wait()
	}, test.EntriesAsFolder("full_sync"))
})
