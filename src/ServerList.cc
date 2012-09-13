/* Copyright (c) 2011-2012 Stanford University
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <unordered_set>

#include "Common.h"
#include "ServerList.h"
#include "ServerTracker.h"
#include "ShortMacros.h"
#include "TransportManager.h"

namespace RAMCloud {

/**
 * Constructor for ServerList.

 * \param context
 *      Overall information about the RAMCloud server.  The constructor
 *      will modify context so that its serverList member refers to this
 *      object.
 */
ServerList::ServerList(Context* context)
    : AbstractServerList(context)
    , serverList()
{
}

/**
 * Destructor for ServerList.
 */
ServerList::~ServerList()
{
}

//////////////////////////////////////////////////////////////////////
// ServerList - Protected Methods (inherited from AbstractServerList)
//////////////////////////////////////////////////////////////////////
ServerDetails*
ServerList::iget(ServerId id)
{
    uint32_t index = id.indexNumber();
    if ((index < serverList.size()) && serverList[index]) {
        ServerDetails* details = serverList[index].get();
        if (details->serverId == id)
            return details;
    }
    return NULL;
}

ServerDetails*
ServerList::iget(uint32_t index)
{
    return (serverList[index]) ? serverList[index].get() : NULL;
}

/**
 * Return the number of valid indexes in this list w/o lock. Valid does not mean
 * that they're occupied, only that they are within the bounds of the array.
 */
size_t
ServerList::isize() const
{
    return serverList.size();
}


//////////////////////////////////////////////////////////////////////
// ServerList Public Methods
//////////////////////////////////////////////////////////////////////
/**
 * Return the ServerId associated with a given index. If there is none,
 * an invalid ServerId is returned (i.e. the isValid() method will return
 * false.
 */
ServerId
ServerList::operator[](uint32_t index)
{
    Lock lock(mutex);
    if (index >= serverList.size() || !serverList[index])
        return ServerId(/* invalid id */);
    return serverList[index]->serverId;
}

/**
 * Apply a server list from the coordinator to the local server list so they
 * are consistent.  In addition, all registered  trackers will receive
 * notification of all events related to servers they are aware of along with
 * notifications for any new servers in the updated list.
 *
 * Outdated/repeated updates are ignored.
 *
 * \param list
 *      A complete snapshot of the coordinator's server list.
 */
void
ServerList::applyServerList(const ProtoBuf::ServerList& list)
{
    // Ignore older updates
    if (list.version_number() <= version) {
        LOG(NOTICE, "A repeated/old update version %lu was sent to "
                "a ServerList with version %lu.",
                list.version_number(), version);
        return;
    }

    LOG(NOTICE, "Server List from coordinator:\n%s",
                list.DebugString().c_str());

    /*
     * This is a demultiplexer for ProtoBuf::Serverlists; Since ServerList
     * membership updates and full list updates have very similar structures
     * their RPCs and ProtoBufs have been combined into one, yet the
     * semantics are different. Therefore, this switch is used to
     * demux the message.
     */
    switch (list.type()) {
        case ProtoBuf::ServerList_Type_UPDATE:
            /*
             * applyUpdate() can only fail if an update was missed which
             * should be impossible unless there was a programmer error in
             * the CoordinatorServerList update management code.
             */
            if (!applyUpdate(list)) {
                DIE("Server List Update failed got update version %lu, but "
                        "expected %lu", list.version_number(), version + 1);
            }
            break;
        case ProtoBuf::ServerList_Type_FULL_LIST:
            applyFullList(list);
            break;
    }
}

//////////////////////////////////////////////////////////////////////
// ServerList - Private Methods
//////////////////////////////////////////////////////////////////////

/**
 * Internal call to apply a full list ProtoBuf from the coordinator. This
 * should only be invoked by applyServerList(). Trakers will be notified of
 * changes to the local server list.
 *
 * See applyServerList() docs for more details.
 *
 * \param list
 *      A complete snapshot of the coordinator's server list.
 */
void
ServerList::applyFullList(const ProtoBuf::ServerList& list)
{
    Lock lock(mutex);
    assert(list.type() == ProtoBuf::ServerList_Type_FULL_LIST);

    LOG(NOTICE, "Got complete list of servers containing %d entries (version "
        "number %lu)", list.server_size(), list.version_number());

    // Build a temporary map of servers currently in the server list
    // so that we can efficiently evict down servers from the list.
    std::unordered_set<uint64_t> listIds;
    foreach (const auto& server, list.server()) {
        ServerStatus status = ServerStatus(server.status());
        if (status == ServerStatus::DOWN) {
            LOG(WARNING, "Coordinator provided server list contains servers "
                "which are down. Ignoring, but this is likely due to a "
                "serious bug and is likely to cause worse bugs: offending "
                "server id %s",
                ServerId(server.server_id()).toString().c_str());
        } else {
            assert(!RAMCloud::contains(listIds, server.server_id()));
            listIds.insert(server.server_id());
        }
    }

    // Order matters here.  First all downs are done for all servers, then all
    // crashes, then all adds.  This is important because when enlisting some
    // servers may "replace" others and a guarantee is given to them that
    // whenever tracker clients become aware of the enlisting server
    // through a tracker event queue that same tracker has already been made
    // aware of the crash event of the server id being replaced.

    // DOWNs are done first.
    foreach (const Tub<ServerDetails>& server, serverList) {
        if (!server)
            continue;
        assert(server->serverId.isValid());
        if (!RAMCloud::contains(listIds, server->serverId.getId())) {
            remove(server->serverId);
        }
    }

    // CRASHED is done next.
    foreach (const auto& server, list.server()) {
        if (ServerStatus(server.status()) != ServerStatus::CRASHED)
            continue;
        crashed(ServerId(server.server_id()), server.service_locator(),
                ServiceMask::deserialize(server.services()),
                server.expected_read_mbytes_per_sec());
    }

    // Finally UPs are done.
    foreach (const auto& server, list.server()) {
        if (ServerStatus(server.status()) != ServerStatus::UP)
            continue;
        add(ServerId(server.server_id()), server.service_locator(),
            ServiceMask::deserialize(server.services()),
            server.expected_read_mbytes_per_sec());
    }

    version = list.version_number();

    foreach (ServerTrackerInterface* tracker, trackers)
        tracker->fireCallback();
}

/**
 * Applies coordinator server list updates to the local server list. Trackers
 * will be notified. This should only be invoked by applyServerList().
 *
 * See applyServerList() docs for more details.
 *
 * \param update
 *      A complete snapshot of the coordinator's server list.
 * \return
 *      false if updates were lost from the coordinator, or
 *      true if \a update was applied successfully.
 */
bool
ServerList::applyUpdate(const ProtoBuf::ServerList& update)
{
    Lock lock(mutex);
    assert(update.type() == ProtoBuf::ServerList_Type_UPDATE);

    // If this isn't the next expected update, request that the entire list
    // be pushed again.
    if (update.version_number() != (version + 1)) {
        LOG(NOTICE, "Update generation number is %lu, but last seen was %lu. "
            "Something was lost! Shouldn't happen unless there\'s a programmer "
            "error in the CoordinatorServerList update management code.",
            update.version_number(), version);
        return false;
    }

    LOG(NOTICE, "Got server list update (version number %lu)",
        update.version_number());

    foreach (const auto& server, update.server()) {
        ServerId id(server.server_id());
        assert(id.isValid());
        ServerStatus status = ServerStatus(server.status());
        const string& locator = server.service_locator();
        ServiceMask services =
            ServiceMask::deserialize(server.services());
        uint32_t readMBytesPerSec = server.expected_read_mbytes_per_sec();
        if (status == ServerStatus::UP) {
            LOG(NOTICE, "  Adding server id %s (locator \"%s\") "
                         "with services %s and %u MB/s storage",
                id.toString().c_str(), locator.c_str(),
                services.toString().c_str(), readMBytesPerSec);
            add(id, locator, services, readMBytesPerSec);
        } else if (status == ServerStatus::CRASHED) {
            if (iget(id) == NULL) {
                LOG(ERROR, "  Cannot mark server id %s as crashed: The server "
                    "is not in our list, despite list version numbers matching "
                    "(%lu). Something is screwed up! Requesting the entire "
                    "list again.", id.toString().c_str(),
                    update.version_number());
                return false;
            }

            LOG(NOTICE, "  Marking server id %s as crashed",
                id.toString().c_str());
            crashed(id, locator, services, readMBytesPerSec);
        } else if (status == ServerStatus::DOWN) {
            if (iget(id) == NULL) {
                LOG(ERROR, "  Cannot remove server id %s: The server is "
                    "not in our list, despite list version numbers matching "
                    "(%lu). Something is screwed up! Requesting the entire "
                    "list again.", id.toString().c_str(),
                    update.version_number());
                return false;
            }

            LOG(NOTICE, "  Removing server id %s", id.toString().c_str());
            remove(id);
        }
    }

    version = update.version_number();

    foreach (ServerTrackerInterface* tracker, trackers)
        tracker->fireCallback();

    return true;
}

/**
 * Add a new server to the ServerList along with some details.
 * All registered ServerTrackers will have the changes enqueued to them.
 * The caller is responsible for firing tracker callbacks if the
 * server list changed in response to this call.
 *
 * Upon successful return the slot in the server list which corresponds to
 * the indexNumber of \a id will reflect the passed in details and with
 * the server having an UP status.
 *
 * \param id
 *      The ServerId of the server to add.
 * \param locator
 *      The service locator of the server to add.
 * \param services
 *      Which services this server provides.
 * \param expectedReadMBytesPerSec
 *      If services.has(BACKUP_SERVICE) then this should describe the storage
 *      performance the server reported when enlisting with the coordiantor,
 *      otherwise the value is ignored.  In MB/s.
 */
bool
ServerList::add(ServerId id, const string& locator,
                ServiceMask services, uint32_t expectedReadMBytesPerSec)
{
    /*
     * Breakdown of the actions this method takes based on the id of the
     * existing server in the same slot id will reside in.
     *           | ids equal  | id is newer than entry
     * ----------+----------------------------------------
     *  Up       | No-op      | Crash, Down current; Up id
     *  Crashed  | Log/Ignore | Down current; Up id
     *  Down     | Columns indistinguishable; Up id
     */
    uint32_t index = id.indexNumber();

    if (index >= serverList.size())
        serverList.resize(index + 1);

    Tub<ServerDetails>& entry = serverList[index];
    if (entry) {
        if (id.generationNumber() < entry->serverId.generationNumber()) {
            // Add of older ServerId; drop it.
            LOG(WARNING, "Dropping addition of ServerId older than the current "
                "entry (%s < %s)!", id.toString().c_str(),
                entry->serverId.toString().c_str());
            return false;
        } else if (id.generationNumber() > entry->serverId.generationNumber()) {
            // Add of newer ServerId; need to play notifications to remove
            // current entry.
            LOG(WARNING, "Addition of %s seen before removal of %s! Issuing "
                "removal before addition.",
                id.toString().c_str(), entry->serverId.toString().c_str());
            remove(entry->serverId);
            // Fall through to do addition.
        } else { // Generations are equal
            if (entry->status == ServerStatus::UP) {
                // Nothing to do; already in the right status.
                LOG(WARNING, "Duplicate add of ServerId %s!",
                    id.toString().c_str());
            } else {
                // Something's not right; shouldn't see an add for a crashed
                // server.
                LOG(WARNING, "Add of ServerId %s after it had already been "
                    "marked crashed; ignoring", id.toString().c_str());
            }
            return false;
        }
    }
    assert(!entry);

    entry.construct(id, locator, services,
                    expectedReadMBytesPerSec, ServerStatus::UP);
    foreach (ServerTrackerInterface* tracker, trackers)
        tracker->enqueueChange(*entry, ServerChangeEvent::SERVER_ADDED);
    return true;
}

/**
 * Mark a server as crashed in the ServerList.
 * All registered ServerTrackers will have the changes enqueued to them.
 * The caller is responsible for firing tracker callbacks if the
 * server list changed in response to this call.
 *
 * The additional arguments besides \a id are used in the case that the
 * server must be marked CRASHED when the server was never marked as UP
 * (in this case the details of the server aren't in the list).
 *
 * Upon successful return the slot in the server list which corresponds to
 * the indexNumber of \a id will reflect the passed in details and with
 * the server having an CRASHED status.
 *
 * \param id
 *      The ServerId of the server to mark as crashed.
 * \param locator
 *      The service locator of the server to add (in the case that the details
 *      of the server were never added).
 * \param services
 *      Which services this server provides (in the case that the details of
 *      the server were never added).
 * \param expectedReadMBytesPerSec
 *      If services.has(BACKUP_SERVICE) then this should describe the storage
 *      performance the server reported when enlisting with the coordiantor,
 *      otherwise the value is ignored.  In MB/s.  Only used in the case that
 *      the details of the server were never added.
 */
bool
ServerList::crashed(ServerId id, const string& locator,
                    ServiceMask services, uint32_t expectedReadMBytesPerSec)
{
    /*
     * Breakdown of the actions this method takes based on the id of the
     * existing server in the same slot id will reside in.
     *           | ids equal  | id is newer than entry
     * ----------+-----------------------------------------------
     *  Up       | Crash      | Crash, Down current; Up, Crash id
     *  Crashed  | No-op      | Down current; Up id, Crash id
     *  Down     | Columns indistinguishable; Up, Crash id
     */
    uint32_t index = id.indexNumber();

    if (index >= serverList.size() || !serverList[index]) {
        // No existing entry; need to add first.
        add(id, locator, services, expectedReadMBytesPerSec);
    }

    Tub<ServerDetails>& entry = serverList[index];
    if (entry) {
        if (id.generationNumber() < entry->serverId.generationNumber()) {
            // Crash of older ServerId; drop it.
            LOG(WARNING, "Dropping crash of ServerId older than the current "
                "entry (%s < %s)!", id.toString().c_str(),
                entry->serverId.toString().c_str());
            return false;
        } else if (id.generationNumber() > entry->serverId.generationNumber()) {
            // Crash of newer ServerId; need to play notifications to remove
            // current entry and add id before marking it as crashed.
            LOG(WARNING, "Crash of %s seen before crash of %s! Issuing "
                "crash/removal before addition.",
                id.toString().c_str(), entry->serverId.toString().c_str());
            remove(entry->serverId);
            // We have a crash event for a server that was never added just
            // make up some unusable details about the server.  No one should
            // ever contact it and if they do the locator won't work.
            add(id, locator, services, expectedReadMBytesPerSec);
            // Fall through to do crash of id.
        } else { // Generations are equal.
            if (entry->status == ServerStatus::CRASHED) {
                // Nothing to do; already in the right status.
                LOG(WARNING, "Duplicate crash of ServerId %s!",
                    id.toString().c_str());
                return false;
            }
            // Fall through to do crash of id.
        }
    }

    // At this point the entry should exist, be for id, and should be up.
    assert(entry);
    assert(entry->serverId == id);
    assert(entry->status == ServerStatus::UP);

    entry->status = ServerStatus::CRASHED;
    foreach (ServerTrackerInterface* tracker, trackers) {
        tracker->enqueueChange(
            ServerDetails(entry->serverId, ServerStatus::CRASHED),
            ServerChangeEvent::SERVER_CRASHED);
    }

    return true;
}

/**
 * Remove a server from the ServerList.
 * All registered ServerTrackers will have the changes enqueued to them.
 * The caller is responsible for firing tracker callbacks if the
 * server list changed in response to this call.
 *
 * Upon successful return the slot in the server list which corresponds to
 * the indexNumber of \a id will be empty (which implies DOWN).
 *
 * \param id
 *      The ServerId of the server to remove from the ServerList.
 */
bool
ServerList::remove(ServerId id)
{
    /*
     * Breakdown of the actions this method takes based on the id of the
     * existing server in the same slot id will reside in.
     *           | ids equal or id is newer than entry
     * ----------+-----------------------------------------------
     *  Up       | Crash, Down current; ignore id
     *  Crashed  | Down current; ignore id
     *  Down     | Ignore id
     */
    uint32_t index = id.indexNumber();

    // If we're told to remove a server we're never heard of, just log
    // and ignore it. This shouldn't happen normally, but could in
    // theory if we never learn of a server and then hear about its
    // demise, or if a short-lived server's addition notification is
    // reordered and arrives after the removal notification, or if a
    // new server that occupies the same index has an addition
    // notification arrive before the previous one's removal.
    if (index >= serverList.size() ||
        !serverList[index] ||
        (id.generationNumber() <
             serverList[index]->serverId.generationNumber())) {
        LOG(WARNING, "Ignoring removal of unknown ServerId %s",
            id.toString().c_str());
        return false;
    }

    ServerDetails& entry = *serverList[index];

    // In theory it's possible we could have missed both a prior removal and
    // the next addition, and then see the removal for something newer than
    // what's stored. Unlikely, but let's log it just in case.
    if (id.generationNumber() > entry.serverId.generationNumber()) {
        LOG(WARNING, "Removing ServerId %s because removal for a newer "
            "generation number was received (%s)",
            entry.serverId.toString().c_str(), id.toString().c_str());
    }

    // Be sure to use the stored id, not the advertised one, just in case
    // we're removing an older entry (see previous comment above).
    if (entry.status == ServerStatus::UP) {
        crashed(entry.serverId, entry.serviceLocator,
                entry.services, entry.expectedReadMBytesPerSec);
    }
    foreach (ServerTrackerInterface* tracker, trackers) {
        tracker->enqueueChange(
            ServerDetails(entry.serverId, ServerStatus::DOWN),
            ServerChangeEvent::SERVER_REMOVED);
    }

    serverList[index].destroy();
    return true;
}

} // namespace RAMCloud
