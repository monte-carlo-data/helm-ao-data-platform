// Check local files only. The startup wrapper suppresses all parser output so
// malformed Secret contents cannot become log messages.
package main

import (
	"os"

	"github.com/Altinity/clickhouse-backup/v2/pkg/config"
)

func main() {
	if len(os.Args) != 2 {
		os.Exit(1)
	}
	cfg, err := config.LoadConfig(os.Args[1])
	if err != nil || config.ValidateServerCredentials(cfg) != nil {
		os.Exit(1)
	}
}
