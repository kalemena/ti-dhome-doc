THIS_FILE := $(lastword $(MAKEFILE_LIST))
WORKSPACE := $(PWD)
SHELL := /bin/bash

.PHONY: 

###########################
# BUILDING & PUBLISHING DOC
###########################

# Builds PDF book
doc.publishToPDF: 
	source .github/docPublishingScripts.sh && publishPDF

# Builds PDF book
doc.publishToHTML: 
	source .github/docPublishingScripts.sh && publishHTML

# Clean caches
doc.clean:
	rm -rf $(CURDIR)/build
	# docker run --rm -v $(CURDIR):/docs alpine rm -rf /docs/build