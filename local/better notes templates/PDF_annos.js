  
  ${
    // https://www.zotero.org/support/note_templates
      await(async () => {
  
          // Helper function to generate hyperlinks to annotations
          const createAnnotationLink = (attachment, annoItem, text) => {
              return `<a href="zotero://open-pdf/0_${attachment.key}?annotation=${annoItem.key}">${text || 'Annotation'}</a>`;
          };

const getAnnotationsFlat = async (_item) => {
    const annots = await _item.getAnnotations();
    const output = [];

    const structuralMarkers = [];
    const pageLocators = {}; // { "1": "Kapitel 3", "2": "..." }
    const annotationEntries = [];

    for (const annoItem of annots) {
        const annoJSON = await Zotero.Annotations.toJSON(annoItem);
        const sortIndex = annoJSON.sortIndex;
        const content = annoJSON.text?.trim() || '';
        const comment = annoItem.annotationComment?.trim() || '';
        const annotationType = annoItem.annotationType;
        const pageLabel = annoJSON.pageLabel;

        const tagsRaw = await annoItem.getTags();
        const tags = tagsRaw.map(t => t.tag.trim()).filter(Boolean);

        // :h1, :h2 etc.
        const headingTag = tags.find(t => /^:h\d$/.test(t));
        if (headingTag) {
            structuralMarkers.push({
                sortIndex,
                level: headingTag.slice(1), // e.g. h1
                title: comment || content
            });
            continue;
        }

        // :page → Seite → benannter Locator
        if (tags.includes(":page") && pageLabel) {
            const locatorText = comment || content;
            if (locatorText) {
                pageLocators[pageLabel] = locatorText;
            }
            continue;
        }

        // Normale Annotation
        annotationEntries.push({
            sortIndex,
            annoItem,
            annoJSON,
            tags,
            comment,
            content,
            annotationType,
            pageLabel
        });
    }

    structuralMarkers.sort((a, b) => a.sortIndex.localeCompare(b.sortIndex));
    annotationEntries.sort((a, b) => a.sortIndex.localeCompare(b.sortIndex));

    let currentHeadingIndex = -1;
    let currentPageLabel = null;

    for (const entry of annotationEntries) {
        const {
            annoItem,
            annoJSON,
            annotationType,
            content,
            comment,
            tags,
            pageLabel
        } = entry;

        // Neue Überschrift (h1/h2) nötig?
        const nextHeading = structuralMarkers[currentHeadingIndex + 1];
        if (nextHeading && entry.sortIndex >= nextHeading.sortIndex) {
            output.push(`<${nextHeading.level}>${nextHeading.title}</${nextHeading.level}>`);
            currentHeadingIndex++;
        }

        // Neue Seite → pageLocator einfügen
        if (pageLabel && pageLabel !== currentPageLabel) {
            currentPageLabel = pageLabel;
            const locator = pageLocators[pageLabel];
            if (locator) {
                output.push(`<code>${locator}</code>`);
            }
        }

        // Tags (ohne Steuer-Tags wie :page, :h1)
        const tagDisplay = tags.filter(t => !/^:/.test(t));
        const tagLine = tagDisplay.length
            ? `Tags: ${tagDisplay.map(t => `<strong>${t}</strong>`).join(", ")}`
            : '';
        const metaParts = [tagLine, comment].filter(Boolean).join("<br>");

        let annotationHTML = "";
        if (
            ["note", "text", "highlight", "underline"].includes(annotationType) &&
            content
        ) {
            annotationHTML = `<blockquote>${content}</blockquote>`;
        }

        const link = `<b>${createAnnotationLink(_item, annoItem, "Link")}</b>`;

        output.push(
            `${annotationHTML}${metaParts ? `<p>${metaParts}<br>${link}</p>` : `<p>${link}</p>`}`
        );
    }

    return output;
};
          let annotationsList = [];
          const attachments = await Zotero.Items.get(topItem.getAttachments()).filter((i) => i.isPDFAttachment() || i.isSnapshotAttachment() || i.isEPUBAttachment());
		  
			for (let attachment of attachments) {
				let annotations = await getAnnotationsFlat(attachment);
				annotationsList = annotationsList.concat(annotations);
			}

  
          // HTML Output
          let res = `${annotationsList.join('\n')}`
		;
          return res;
      })()
  }