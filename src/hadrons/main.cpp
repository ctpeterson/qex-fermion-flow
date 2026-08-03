#include <Hadrons/Application.hpp>
#include <Hadrons/Modules.hpp>

using namespace Grid;
using namespace Hadrons;

//////////////////////////////////////////
// Parameters class for XML input
//////////////////////////////////////////

class FlowBilinearsPar: Serializable {
public:
  GRID_SERIALIZABLE_CLASS_MEMBERS(
    FlowBilinearsPar,
    std::string,  gaugeFile,    // NERSC config stem; empty -> unit gauge
    unsigned int, Ls,           // DWF fifth-dimension extent
    double,       M5,           // DWF domain-wall height
    double,       mass,         // quark mass
    std::string,  boundary,     // fermion BCs, e.g. "1 1 1 -1"
    std::string,  twist,        // fermion twists, e.g. "0. 0. 0. 0."
    double,       residual,     // CG stopping residual
    unsigned int, maxIteration, // CG max iterations
    unsigned int, numSources,   // number of Z2 noise hits
    unsigned int, flowSteps,    // number of flow integration steps
    double,       flowStepSize, // flow step size in units of a^2
    unsigned int, measInterval, // measure every measInterval steps
    int,          flowBC,       // fermion flow time BC (+1/-1)
    std::string,  bilinears,    // bilinear list, e.g. "all" or "Gamma5 dslash"
    std::string,  output
  );
};

static std::string flowTimeTag(const double t) { 
  std::stringstream ss; 
  ss << std::fixed << std::setprecision(2) << t;
  return ss.str();
}

//////////////////////////////////////////
// Flow execution
///////////////////////////////////////////

int main(int argc, char *argv[]) {
  //////////////////////////////////////////
  // Command line
  ///////////////////////////////////////////
  std::string parameterFileName;
    
  if (argc < 2) {
    std::cerr << "usage: " << argv[0] << " <parameter file> [Grid options]";
    std::cerr << std::endl;
    std::exit(EXIT_FAILURE);
  }
  parameterFileName = argv[1];
    
  //////////////////////////////////////////
  // Initialization
  ///////////////////////////////////////////
  Grid_init(&argc, &argv);
    
  HadronsLogError.Active(GridLogError.isActive());
  HadronsLogWarning.Active(GridLogWarning.isActive());
  HadronsLogMessage.Active(GridLogMessage.isActive());
  HadronsLogIterative.Active(GridLogIterative.isActive());
  HadronsLogDebug.Active(GridLogDebug.isActive());
    
  const unsigned int nt = GridDefaultLatt()[Tp];

  LOG(Message) << "Grid initialized" << std::endl;

  //////////////////////////////////////////
  // Application initialization
  ///////////////////////////////////////////
  Application application;
  Application::GlobalPar globalPar;
  FlowBilinearsPar bilinearsPar;
    
  { // reading parameters
    XmlReader reader(parameterFileName);
    read(reader, "global", globalPar);
    read(reader, "flowBilinears", bilinearsPar);
  }

  // global initialization
  application.setPar(globalPar);

  //////////////////////////////////////////
  // Module setup
  ///////////////////////////////////////////
  std::vector<std::string> results;

  // gauge field IO module
  if (bilinearsPar.gaugeFile.empty())
  { application.createModule<MGauge::Unit>("gauge"); }
  else {
    MIO::LoadNersc::Par loadPar;
    loadPar.file = bilinearsPar.gaugeFile;
    application.createModule<MIO::LoadNersc>("gauge", loadPar);
  }

  // DWF action module
  MAction::DWF::Par actionPar;
  actionPar.gauge    = "gauge";
  actionPar.Ls       = bilinearsPar.Ls;
  actionPar.M5       = bilinearsPar.M5;
  actionPar.mass     = bilinearsPar.mass;
  actionPar.boundary = bilinearsPar.boundary;
  actionPar.twist    = bilinearsPar.twist;
  application.createModule<MAction::DWF>("DWF", actionPar);

  // DWF solver module
  MSolver::RBPrecCG::Par solverPar;
  solverPar.action       = "DWF";
  solverPar.residual     = bilinearsPar.residual;
  solverPar.maxIteration = bilinearsPar.maxIteration;
  application.createModule<MSolver::RBPrecCG>("CG", solverPar);

  // noise sources & propagators
  std::vector<std::string> etaName, qName;
  for (unsigned int i = 0; i < bilinearsPar.numSources; ++i) {
    // full-volume Z2 noise source module
    MSource::Z2::Par z2Par;
    z2Par.tA = 0;
    z2Par.tB = nt - 1;
    etaName.push_back("eta_" + std::to_string(i));
    application.createModule<MSource::Z2>(etaName[i], z2Par);

    // propagator module
    MFermion::GaugeProp::Par quarkPar;
    quarkPar.solver = "CG";
    quarkPar.source = etaName[i];
    qName.push_back("Q_" + std::to_string(i));
    application.createModule<MFermion::GaugeProp>(qName[i], quarkPar);
  }

  { // zero-flow-time bilinears module
    MContraction::FermionBilinears::Par bilinearPar;
    bilinearPar.sources     = etaName;
    bilinearPar.propagators = qName;
    bilinearPar.bilinears   = bilinearsPar.bilinears;
    bilinearPar.gauge       = "gauge";
    application.createModule<MContraction::FermionBilinears>("bilinears_t0.00", bilinearPar);
    results.push_back("bilinears_t0.00");
  }

  // gradient flow module
  MGradientFlow::WilsonFermionFlow::Par flowPar;
  flowPar.gauge         = "gauge";
  flowPar.steps         = bilinearsPar.flowSteps;
  flowPar.step_size     = bilinearsPar.flowStepSize;
  flowPar.meas_interval = bilinearsPar.measInterval;
  flowPar.bc            = bilinearsPar.flowBC;
  flowPar.props         = etaName;
  flowPar.props.insert(flowPar.props.end(), qName.begin(), qName.end());
  application.createModule<MGradientFlow::WilsonFermionFlow>("flow", flowPar);

  // bilinears at each flow measurement time
  for (unsigned int i = 1; i <= bilinearsPar.flowSteps; ++i) {
    if ((i % bilinearsPar.measInterval == 0) || (i == bilinearsPar.flowSteps)) {
      std::string tag = flowTimeTag(bilinearsPar.flowStepSize*i);

      // finite-flow-time bilinears module
      MContraction::FermionBilinears::Par bilinearPar;
      for (auto &eta: etaName) bilinearPar.sources.push_back(eta + "_t" + tag);
      for (auto &q: qName)     bilinearPar.propagators.push_back(q + "_t" + tag);
      bilinearPar.bilinears = bilinearsPar.bilinears;
      bilinearPar.gauge     = "flow_U_t" + tag;
      application.createModule<MContraction::FermionBilinears>("bilinears_t" + tag, bilinearPar);
      results.push_back("bilinears_t" + tag);
  } }

  // output module
  MIO::WriteResultGroup::Par writePar;
  writePar.results = results;
  writePar.output  = bilinearsPar.output;
  application.createModule<MIO::WriteResultGroup>("writeResults", writePar);

  //////////////////////////////////////////
  // Execution
  ///////////////////////////////////////////
  try { application.run(); }
  catch (const std::exception& e) { Exceptions::abort(e); }
    
  //////////////////////////////////////////
  // Finalization
  ///////////////////////////////////////////
  LOG(Message) << "Grid is finalizing now" << std::endl;

  Grid_finalize();
    
  return EXIT_SUCCESS;
}
