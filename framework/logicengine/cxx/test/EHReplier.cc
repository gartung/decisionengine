#include <memory>
#include <sstream>
#include <string>
#include <time.h>

#include <rms/RmsReceiver.h>
#include <rms/RmsSender.h>
#include <rms/base/RmsDestination.h>
#include <rms/provider/DDSConnection.h>

#include <NovaTimingUtilities/TimingUtilities.h>

#include <DAQMessages/ccpp_ErrorHandlerMessages.h>

using namespace gov::fnal::cd::rms;
using namespace novadaq::timeutils;

// just send a reply message

int
main()
{

  // create the connection to RMS
  std::shared_ptr<provider::DDSConnection> rmsConnection(
    new provider::DDSConnection("EHTestApplication", 0));

  // create the destination that represents where we will receive
  // messages from
  base::RmsDestination receiveDest("EHServerMailbox",
                                   base::RmsDestination::EH_CHANNEL);

  RmsSender<provider::DDSConnection, errorhandlermessages::ErrorHandlerReply>
    requestSender(rmsConnection, receiveDest);

  errorhandlermessages::ErrorHandlerReply reply;
  reply.action_complete = true;
  requestSender.sendMessage(reply);
  requestSender.close();

  // cleanup and exit
  rmsConnection->close();

  return 0;
}
