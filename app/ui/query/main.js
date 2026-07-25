//
// Place any custom JS here
//

// reference to the sparnatural webcomponent
const sparnatural = document.querySelector("spar-natural");

const params = new URLSearchParams(window.location.search);
const endpointFromUrl = params.get("endpoint");
const endpoint = endpointFromUrl || sparnatural.getAttribute("endpoint");

sparnatural.setAttribute("endpoint", endpoint);

const displayEndpoint = document.querySelector("#displayEndpoint");
displayEndpoint.setAttribute("href", endpoint);
displayEndpoint.textContent = endpoint;

const yasqe = new Yasqe(document.getElementById("yasqe"), {
  requestConfig: {
    endpoint,
    method: "GET",
    header: {}
  },
  copyEndpointOnNewTab: false
});

// init yasR result display
// register a specific plugin that is capable of displaying clikable label + URI
Yasr.registerPlugin("TableX",SparnaturalYasguiPlugins.TableX);
Yasr.registerPlugin("Grid",SparnaturalYasguiPlugins.GridPlugin);
Yasr.registerPlugin("Stats",SparnaturalYasguiPlugins.StatsPlugin);
Yasr.registerPlugin("Map",SparnaturalYasguiPlugins.MapPlugin);

delete Yasr.plugins['table'];
delete Yasr.plugins['response'];

const yasr = new Yasr(document.getElementById("yasr"), {
	pluginOrder: ["TableX", "Grid", "Stats", "Map"],
	defaultPlugin: "TableX",
	persistencyExpire: 0,
	maxPersistentResponseSize: 0
  });

// link yasqe and yasr
yasqe.on("queryResponse", function(_yasqe, response, duration) {
	yasr.setResponse(response, duration);
	// when response is received, enable the button
	sparnatural.enablePlayBtn();
});


// sparnatural.addEventListener("init", (event) => {
// 	// notify the specification to yasr plugins
// 	for (const plugin in yasr.plugins) {
// 	  if (yasr.plugins[plugin].notifyConfiguration) {
// 	    yasr.plugins[plugin].notifyConfiguration(
// 	      event.detail.config
// 	    );
// 	  }
// 	}
// });

document.getElementById('export').onclick = function(event) {
	event.preventDefault();
	const jsonString = JSON.stringify(JSON.parse(document.getElementById('query-json').value), null, 2);
	document.getElementById('export-json').value = jsonString;
	new bootstrap.Modal(document.getElementById('exportModal')).show();
};

// // listener when sparnatural updates the query
// // see http://docs.sparnatural.eu/Javascript-integration.html#sparnatural-events
// sparnatural.addEventListener("queryUpdated", (event) => {
// 	// get the SPARQL query string, and pass it to yasQE
// 	const queryString = sparnatural.expandSparql(event.detail.queryString);
// 	yasqe.setValue(queryString);
// 	// display the JSON query on the console
// 	console.log("Sparnatural JSON query structure:");
// 	console.dir(event.detail.queryJson);

// 	// notify the query to yasr plugins
// 	for (const plugin in yasr.plugins) {
// 	  if (yasr.plugins[plugin].notifyQuery) {
// 	    yasr.plugins[plugin].notifyQuery(event.detail.queryJson);
// 	  }
// 	}
// });

// // listener when the sparnatural submit button is clicked
// // see http://docs.sparnatural.eu/Javascript-integration.html#sparnatural-events
// sparnatural.addEventListener("submit", (event) => {
// 	// enable loader on button
// 	sparnatural.disablePlayBtn() ; 
// 	// trigger the query from YasQE
// 	yasqe.query();
// });

// // listener when the sparnatural reset button is clicked
// // see http://docs.sparnatural.eu/Javascript-integration.html#sparnatural-events
// sparnatural.addEventListener("reset", (event) => {
// 	// empties the SPARQL query on yasQE
// 	yasqe.setValue("");
// });

const tableXConfig = yasr.plugins["TableX"];
Object.assign(tableXConfig.config, {
  includeControls: true,
  openIriInNewWindow: true,

  uriHrefAdapter: (uri) =>
    `/ui/browse/resource/#${encodeURIComponent(uri)}`
});
tableXConfig.persistentConfig.compact = true;


const importModal = new bootstrap.Modal(document.getElementById('importModal'));

document.getElementById('import').addEventListener('click', function(event) {
	event.preventDefault();
	importModal.show();
});

document.getElementById('importButton').addEventListener('click', function() {
	const importJson = document.getElementById('import-json').value;
	const json = JSON.parse(importJson);
	importModal.hide();
	sparnatural.loadQuery(json);
});


// hide/show yasQE
document.getElementById('sparql-toggle').onclick = function() {
	if(document.getElementById('yasqe').style.display == 'none') {
	  document.getElementById('yasqe').style.display = 'block';
	  yasqe.setValue(yasqe.getValue());
	  yasqe.refresh();
	} else {
	  document.getElementById('yasqe').style.display = 'none';
	}
	return false;        
} ;
bindSparnaturalWithYasrPlugins(sparnatural, yasr);
bindSparnaturalWithItself(sparnatural, yasqe, yasr);